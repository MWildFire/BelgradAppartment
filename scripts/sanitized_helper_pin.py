"""Pinned source-helper set and the bootstrap that runs it on the source host.

Receiver-side only: the source helper never imports this module, so it is not part of the set it
pins. The pin covers every repository module the source helper imports at run time, as one set.
Changing any of those files changes HELPER_SET_SHA256, and the server copy must be redeployed
with the new bytes before the receiver can run it.
"""

import hashlib

# Modules the source helper runs on the source host (closure of its imports under scripts/).
HELPER_FILES = (
    "scripts/data_only_backup.py",
    "scripts/data_only_preflight.py",
    "scripts/offhost_backup.py",
    "scripts/protected_recovery.py",
    "scripts/recovery.py",
    "scripts/sanitized_backup.py",
    "scripts/sanitized_contract.py",
)
HELPER_SET_SHA256 = "5bb88529632ecbbb950e2897652c48709fe6a12102a66d021479121bd611b8f2"


def helper_set_digest(files):
    """SHA-256 of 'name' and 'sha256(bytes)' lines in sorted order; HELPER_BOOT matches it."""
    text = "".join(f"{name}\n{hashlib.sha256(files[name]).hexdigest()}\n" for name in sorted(files))
    return hashlib.sha256(text.encode()).hexdigest()


# Runs as: python -I -c HELPER_BOOT <tools_dir> <set_sha256> <names_json> <entry> <helper args...>
# Each file is opened without following links, checked for owner and mode, read once into memory,
# and hashed. Only those bytes are compiled and run; nothing is imported from the directory.
HELPER_BOOT = r"""
import hashlib, importlib, importlib.abc, importlib.util, json, os, re, stat, sys

def _refuse(reason):
    print(json.dumps({"result": "blocked", "reason": reason}))
    raise SystemExit(1)

def _open(directory, name, is_directory):
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if is_directory else 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except OSError:
        _refuse("remote_helper_unavailable")
    status = os.fstat(descriptor)
    kind = stat.S_ISDIR(status.st_mode) if is_directory else stat.S_ISREG(status.st_mode)
    if not kind or status.st_uid not in (0, os.geteuid()) or status.st_mode & 0o022:
        os.close(descriptor)
        _refuse("remote_helper_mode_unsafe")
    return descriptor

def _ancestors_safe(directory):
    # Every directory above the tools directory, with links resolved, must belong to root or the
    # operator, and must not be writable by others unless it is sticky, as /tmp is.
    current = directory
    while True:
        status = os.stat(current)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid not in (0, os.geteuid())
            or (status.st_mode & 0o022 and not status.st_mode & stat.S_ISVTX)
        ):
            _refuse("remote_helper_mode_unsafe")
        if current == "/":
            return
        current = os.path.dirname(current)

try:
    tools, expected, names, entry = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4]
except (IndexError, ValueError):
    _refuse("invalid_remote_helper_set")
if not (
    isinstance(names, list)
    and all(isinstance(n, str) and re.fullmatch(r"scripts/[a-z_]+\.py", n) for n in names)
    and names == sorted(set(names))
    and entry.replace(".", "/") + ".py" in names
):
    _refuse("invalid_remote_helper_set")

parent = os.path.realpath(os.path.dirname(os.path.abspath(tools)))
_ancestors_safe(parent)
root = _open(None, os.path.join(parent, os.path.basename(tools)), True)
scripts = _open(root, "scripts", True)
files = {}
for name in names:
    descriptor = _open(scripts, name.split("/", 1)[1], False)
    with os.fdopen(descriptor, "rb") as handle:
        files[name] = handle.read()
os.close(scripts)
os.close(root)
text = "".join(f"{n}\n{hashlib.sha256(files[n]).hexdigest()}\n" for n in sorted(files))
if hashlib.sha256(text.encode()).hexdigest() != expected:
    _refuse("remote_helper_digest_mismatch")

class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        if module.__name__ == "scripts":
            module.__path__ = []
            return
        key = module.__name__.replace(".", "/") + ".py"
        module.__file__ = "/nonexistent/otz-verified-helper/" + key
        exec(compile(files[key], "<verified " + key + ">", "exec"), module.__dict__)

class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "scripts":
            return importlib.util.spec_from_loader(fullname, _Loader(), is_package=True)
        if fullname.startswith("scripts."):
            if fullname.replace(".", "/") + ".py" not in files:
                raise ImportError("module outside the pinned helper set")
            return importlib.util.spec_from_loader(fullname, _Loader())
        return None

sys.meta_path.insert(0, _Finder())
sys.argv = ["sanitized_backup.py", *sys.argv[5:]]
module = importlib.import_module(entry)
raise SystemExit(module.main())
"""
