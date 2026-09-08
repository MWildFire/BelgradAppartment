#!/usr/bin/env python3
"""Daily private off-host generations; transfer deltas, verify every file, then promote.

No defaults for SSH identity/destination. Never copies protected credentials into data
backups. LaunchAgent generation prints a plist; installing it remains an operator action.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import re
import select
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

try:
    from scripts import recovery
except ModuleNotFoundError:
    import recovery


def run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=7200)


def ssh(args):
    recovery.require(
        re.fullmatch(r"[A-Za-z0-9_.@:-]+", args.ssh_target), "invalid explicit SSH target"
    )
    recovery.require(not args.ssh_target.startswith("-"), "invalid SSH option as target")
    recovery.require(args.ssh_key.is_file(), "explicit SSH key unavailable")
    return [
        "/usr/bin/ssh",
        "-i",
        str(args.ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        args.ssh_target,
    ]


def prefix(args):
    user = getattr(args, "remote_user", None)
    if user:
        recovery.require(re.fullmatch(r"[a-z_][a-z0-9_-]*", user), "invalid service user")
        return ["sudo", "-n", "-u", user, "--"]
    return []


def remote(args, command):
    return run([*ssh(args), shlex.join([*prefix(args), *[str(part) for part in command]])])


@contextmanager
def remote_lease(args):
    # A separate SSH connection owns flock throughout hashing, transfer and verification.
    # Disconnect/EOF releases it; a second Mac/LaunchAgent cannot race this generation.
    code = (
        "import fcntl,sys; f=open(sys.argv[1],'a+'); "
        "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); "
        "print('ready',flush=True); sys.stdin.read()"
    )
    lock = str(args.remote_root / "data/backups/offhost.lock")
    process = subprocess.Popen(
        [*ssh(args), shlex.join([*prefix(args), args.remote_python, "-c", code, lock])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready, _, _ = select.select([process.stdout], [], [], 30)
        recovery.require(
            ready and process.stdout.readline().strip() == b"ready",
            "remote backup lock unavailable",
        )
        yield
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


def atomic_json(path, value):
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(path)


def completed(root):
    accepted = []
    for path in (root / "generations").glob("*"):
        receipt_path = root / "receipts" / f"{path.name}.json"
        try:
            receipt = json.loads(receipt_path.read_text())
            manifest = path / "manifest.json"
            if (
                path.is_dir()
                and not path.is_symlink()
                and receipt.get("result") == "data_restore_verified"
                and receipt.get("generation_manifest_sha256") == recovery.digest(manifest)
            ):
                stamp = datetime.fromisoformat(receipt["created_at"])
                if stamp.tzinfo is not None:
                    accepted.append((stamp, path, receipt))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(accepted, key=lambda entry: entry[0])


def transfer(args, remote_stage, pending, seed):
    transport = shlex.join(ssh(args)[:-1])
    remote_target = args.ssh_target
    remote_rsync = ["--rsync-path", shlex.join([*prefix(args), "/usr/bin/rsync"])]
    run(
        [
            "/usr/bin/rsync",
            "-rt",
            *remote_rsync,
            "-e",
            transport,
            f"{remote_target}:{remote_stage}/",
            str(pending) + "/",
        ]
    )
    manifest = json.loads((pending / "manifest.json").read_text())
    recovery.require(manifest["source_project_root"] == str(args.remote_root), "wrong remote root")
    seed_manifest = json.loads((seed / "manifest.json").read_text())
    transferred = set()
    for role, component in manifest["components"].items():
        archive = str(recovery.safe_relative(component["archive"]))
        source = Path(component["source"])
        recovery.require(
            source.is_relative_to(args.remote_root / "data"), "media outside remote data"
        )
        if role == "state" or archive in transferred:
            continue
        transferred.add(archive)
        files = [
            name[len(archive) + 1 :] for name in manifest["files"] if name.startswith(archive + "/")
        ]
        destination = pending / archive
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        changed = []
        for name in files:
            recovery.safe_relative(name)
            relative = f"{archive}/{name}"
            original, target = seed / relative, pending / relative
            if (
                seed_manifest["files"].get(relative) == manifest["files"][relative]
                and original.is_file()
                and not original.is_symlink()
                and original.resolve().is_relative_to(seed)
            ):
                # Archive extraction does not preserve mtimes. Match by declared content,
                # never by rsync's timestamp shortcut; strict final hashing proves the link.
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.link(original, target)
            else:
                changed.append(name)
        if not changed:
            continue
        files = changed
        file_list = pending.parent / f"{pending.name}-files"
        file_list.write_bytes(b"\0".join(name.encode() for name in files) + b"\0")
        file_list.chmod(0o600)
        try:
            destination = pending / archive
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            command = [
                "/usr/bin/rsync",
                "-rt",
                *remote_rsync,
                "-I",  # these files differ by SHA even if remote mtime/size were reused
                "-0",
                "--files-from",
                str(file_list),
                "--link-dest",
                str(seed / archive),
                "-e",
                transport,
                f"{remote_target}:{source}/",
                str(destination) + "/",
            ]
            run(command)
        finally:
            file_list.unlink(missing_ok=True)
    return manifest


def backup(args):
    root = args.private_root.resolve()
    recovery.private_dir(root)
    for name in ("generations", "pending", "receipts", "logs"):
        recovery.private_dir(root / name)
    with (root / "backup.lock").open("a+") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"result": "skipped_locked"}
        accepted = completed(root)
        now = datetime.now(UTC)
        if accepted and 0 <= (now - accepted[-1][0]).total_seconds() < args.interval_hours * 3600:
            return {"result": "skipped_not_due", "created_at": accepted[-1][0].isoformat()}
        seed = accepted[-1][1] if accepted else args.seed_dir
        recovery.require(
            seed is not None and seed.is_dir() and not seed.is_symlink(),
            "first run requires retained verified extraction via --seed-dir",
        )
        recovery.require((seed / "manifest.json").is_file(), "seed manifest missing")
        recovery.require(
            seed.stat().st_dev == root.stat().st_dev,
            "seed must share filesystem for immutable hardlinks",
        )
        recovery.require(
            shutil.disk_usage(root).free >= args.min_free_gib * 1024**3,
            "insufficient off-host free space; no completed backup removed",
        )
        identifier = now.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8]
        pending = root / "pending" / identifier
        pending.mkdir(mode=0o700)
        stage = args.remote_root / "data/backups" / f".offhost-{uuid4().hex}"
        with remote_lease(args):
            receipt = None
            try:
                command = [
                    args.remote_python,
                    str(args.remote_root / "scripts/recovery.py"),
                    "snapshot",
                    "--output-dir",
                    str(stage),
                    "--project-root",
                    str(args.remote_root),
                    "--db",
                    str(args.remote_root / "data/otz.db"),
                ]
                for role in ("state", "inbound", "listings"):
                    command += [f"--{role}-dir", str(args.remote_root / "data" / role)]
                command += ["--published-dir", str(args.remote_published_dir)]
                remote(args, command)
                transfer(args, stage, pending, seed.resolve())
                manifest = recovery.verify_directory(pending)
                receipt = recovery.verify_application(pending, manifest)
                # This proof is data-only. Protected session/config recovery has its own gate.
                receipt.update(
                    backup_format="directory_generation",
                    manifest_sha256=receipt["generation_manifest_sha256"],
                    offhost_verified=True,
                    generation_id=identifier,
                    protected_recovery_verified=False,
                )
                destination = root / "generations" / identifier
                pending.rename(destination)
                atomic_json(root / "receipts" / f"{identifier}.json", receipt)
                # Only accepted generations participate; failed/pending work never displaces one.
                for _, old, _receipt in completed(root)[:-2]:
                    shutil.rmtree(old)
                    (root / "receipts" / f"{old.name}.json").unlink()
                return receipt
            finally:
                # The remote path is generated here; never remove any source directory.
                try:
                    remote(
                        args,
                        [
                            args.remote_python,
                            "-c",
                            "import shutil,sys; shutil.rmtree(sys.argv[1],ignore_errors=True)",
                            str(stage),
                        ],
                    )
                except (OSError, subprocess.SubprocessError):
                    if receipt is not None:
                        receipt["remote_cleanup"] = "failed_requires_review"
                        atomic_json(root / "receipts" / f"{identifier}.json", receipt)
                    else:
                        print('{"remote_cleanup":"failed_requires_review"}', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "launch-agent"))
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--ssh-key", required=True, type=Path)
    parser.add_argument("--private-root", required=True, type=Path)
    parser.add_argument("--remote-root", required=True, type=Path)
    parser.add_argument("--remote-python", required=True)
    parser.add_argument("--remote-user", help="run remote reads as this service user via sudo -n")
    parser.add_argument("--remote-published-dir", required=True, type=Path)
    parser.add_argument("--seed-dir", type=Path)
    parser.add_argument("--interval-hours", type=float, default=24)
    parser.add_argument("--min-free-gib", type=float, default=20)
    args = parser.parse_args()
    recovery.require(
        args.remote_root.is_absolute() and args.private_root.is_absolute(),
        "absolute roots required",
    )
    recovery.require(
        args.remote_published_dir.is_relative_to(args.remote_root / "data"),
        "published root outside data",
    )
    recovery.require(
        0 < args.interval_hours <= 24 and args.min_free_gib > 0,
        "invalid cadence/free-space threshold",
    )
    if args.command == "launch-agent":
        argv = [sys.executable, str(Path(__file__).resolve()), "run", *sys.argv[2:]]
        payload = {
            "Label": "com.mvstrike.rentbelgrad-backup",
            "ProgramArguments": argv,
            "RunAtLoad": True,
            "StartInterval": 21600,
            "ProcessType": "Background",
            "StandardOutPath": str(args.private_root / "logs/backup.log"),
            "StandardErrorPath": str(args.private_root / "logs/backup-error.log"),
        }
        sys.stdout.buffer.write(plistlib.dumps(payload))
    else:
        try:
            print(json.dumps(backup(args), sort_keys=True))
        except (
            recovery.RecoveryError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ):
            print(json.dumps({"result": "backup_failed", "accepted": False}), file=sys.stderr)
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
