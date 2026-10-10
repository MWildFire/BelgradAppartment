#!/usr/bin/env python3
"""Daily private off-host generations; transfer deltas, verify every file, then promote.

No defaults for SSH identity/destination. Never copies protected credentials into data
backups. LaunchAgent generation prints a plist; installing it remains an operator action.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import plistlib
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

if __package__:
    from scripts import protected_recovery as protected
    from scripts import recovery
else:
    import protected_recovery as protected
    import recovery


_LEASE = ContextVar("offhost_source_lease", default=None)
# Markers of this driver's generations. The sanitized and data-only drivers write other markers.
PLAIN_BACKUP_FORMAT = "directory_generation"


@contextmanager
def backup_phase(name):
    """Tag an operation failure; its context preserves it if cleanup also raises."""
    try:
        yield
    except Exception as error:
        if not hasattr(error, "_offhost_phase"):
            error._offhost_phase = name
        raise


def in_phase(name, operation, *args, **kwargs):
    with backup_phase(name):
        return operation(*args, **kwargs)


def failure_details(error):
    """Never include exception text, commands, paths, streams or environment values."""
    # A context manager's exit can replace the operation exception (e.g. broken SSH
    # stdin while releasing the lease). Report its original tagged context, without
    # changing exception propagation or trusting unrelated untagged exception text.
    selected = error
    current = error
    seen = set()
    for _ in range(16):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if hasattr(current, "_offhost_phase"):
            selected = current
        current = current.__context__
    error = selected
    return {
        "phase": getattr(error, "_offhost_phase", "backup_preflight"),
        "error_type": type(error).__name__,
        "returncode": error.returncode
        if isinstance(error, subprocess.CalledProcessError) and type(error.returncode) is int
        else None,
    }


def assert_lease():
    owner = _LEASE.get()
    recovery.require(owner is None or owner.poll() is None, "source backup lease was lost")


def run(command, *, timeout=7200, input_text=None):
    assert_lease()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            assert_lease()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(input=input_text, timeout=min(1, remaining))
                break
            except subprocess.TimeoutExpired:
                pass
            finally:
                input_text = None
        assert_lease()
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except BaseException:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
        raise


def guarded_child(command, *, timeout, **_kwargs):
    return run(command, timeout=timeout)


def safe_remote_path(path, root):
    value = str(path)
    recovery.require(
        Path(value).is_absolute()
        and ".." not in Path(value).parts
        and re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
        and Path(value).is_relative_to(root),
        "unsafe remote source path",
    )
    return value


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
        token = _LEASE.set(process)
        try:
            assert_lease()
            yield
            assert_lease()
        finally:
            _LEASE.reset(token)
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


def atomic_json(path, value):
    """Replace path with JSON: flush the temporary file, rename it, then flush the directory.

    A power loss keeps the old file or the new one, never a torn one. This is a plain fsync: the
    source host has no F_FULLFSYNC, and receiver copies are checked by digest before use.
    """
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(value, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def completed(root):
    accepted = []
    now = datetime.now(UTC)
    for path in (root / "generations").glob("*"):
        receipt_path = root / "receipts" / f"{path.name}.json"
        try:
            receipt = json.loads(receipt_path.read_text())
            manifest_path = path / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            stamp = datetime.fromisoformat(receipt["created_at"])
            recorded = datetime.fromisoformat(manifest["created_at"])
            if (
                path.is_dir()
                and not path.is_symlink()
                # Only this driver's generations. Sanitized and data-only generations also carry
                # data_restore_verified; without these markers rotation would delete them.
                and receipt.get("backup_format") == PLAIN_BACKUP_FORMAT
                and manifest.get("format") == recovery.FORMAT
                and "scope" not in manifest
                and receipt.get("result") == "data_restore_verified"
                and receipt.get("generation_manifest_sha256") == recovery.digest(manifest_path)
                and stamp.tzinfo is not None
                and recorded.tzinfo is not None
                and stamp == recorded
                and stamp <= now
            ):
                accepted.append((stamp, path, receipt))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(accepted, key=lambda entry: entry[0])


def transfer(args, remote_stage, pending, seed):
    assert_lease()
    safe_remote_path(remote_stage, args.remote_root / "data/backups")
    transport = shlex.join(ssh(args)[:-1])
    remote_target = args.ssh_target
    remote_rsync = ["--rsync-path", shlex.join([*prefix(args), "/usr/bin/rsync"])]
    in_phase(
        "transfer_data_snapshot",
        run,
        [
            "/usr/bin/rsync",
            "-rt",
            *remote_rsync,
            "-e",
            transport,
            f"{remote_target}:{remote_stage}/",
            str(pending) + "/",
        ],
    )
    manifest = json.loads((pending / "manifest.json").read_text())
    recovery.require(manifest["source_project_root"] == str(args.remote_root), "wrong remote root")
    seed_manifest = json.loads((seed / "manifest.json").read_text())
    transferred = set()
    for role, component in manifest["components"].items():
        archive = str(recovery.safe_relative(component["archive"]))
        source = Path(safe_remote_path(component["source"], args.remote_root / "data"))
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
            assert_lease()
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
                # Unchanged files were hardlinked above. Changed files must come
                # from the source; openrsync retains link-dest descriptors and
                # exhausts launchd's file limit on large changed batches.
                "-e",
                transport,
                f"{remote_target}:{source}/",
                str(destination) + "/",
            ]
            in_phase("transfer_media", run, command)
        finally:
            file_list.unlink(missing_ok=True)
    return manifest


def prepared_snapshot_options(args):
    stage = getattr(args, "prepared_snapshot_dir", None)
    checksum = getattr(args, "prepared_snapshot_manifest_sha256", None)
    recovery.require(bool(stage) == bool(checksum), "prepared snapshot requires path and hash")
    if stage is not None:
        safe_remote_path(stage, args.remote_root / "data/backups")
        recovery.require(
            stage.parent == args.remote_root / "data/backups"
            and re.fullmatch(r"\.(?:manual-|offhost-)[A-Za-z0-9_-]{1,100}", stage.name)
            and re.fullmatch(r"[0-9a-f]{64}", checksum),
            "invalid prepared snapshot identity",
        )
    return stage, checksum


def check_prepared_snapshot(args, stage, checksum):
    response = remote(
        args,
        [
            args.remote_python,
            str(
                getattr(args, "remote_recovery_script", None)
                or args.remote_root / "scripts/recovery.py"
            ),
            "check-snapshot-metadata",
            str(stage),
            "--manifest-sha256",
            checksum,
            "--project-root",
            str(args.remote_root),
        ],
    )
    metadata = json.loads(response.stdout)
    recovery.require(
        metadata.get("result") == "prepared_metadata_checked_not_restored"
        and metadata.get("manifest_sha256") == checksum,
        "prepared metadata check not confirmed",
    )
    assert_lease()


def protected_configuration(args):
    files = (getattr(args, "protected_env_file", None), getattr(args, "protected_caddy_file", None))
    sessions = getattr(args, "protected_session", None) or []
    enabled = bool(any(files) or sessions)
    recovery.require(
        not enabled or (all(files) and sessions), "complete protected configuration required"
    )
    recovery.require(
        not getattr(args, "publish_health_receipt", False) or enabled,
        "health publication requires fresh protected recovery",
    )
    return enabled, protected.session_specs(sessions) if enabled else {}


def protected_arguments(args):
    result = copy.copy(args)
    result.health_service_user = getattr(args, "remote_user", None) or "otz"
    result.remote_user = getattr(args, "protected_remote_user", None) or getattr(
        args, "remote_user", None
    )
    return result


def protected_script(args):
    return getattr(args, "remote_protected_recovery_script", None) or Path(
        getattr(args, "remote_recovery_script", None) or args.remote_root / "scripts/recovery.py"
    ).with_name("protected_recovery.py")


def transfer_protected(args, source, destination):
    safe_remote_path(source, args.remote_root / "data/backups")
    assert_lease()
    run(
        [
            "/usr/bin/rsync",
            "-rpt",
            "--rsync-path",
            shlex.join([*prefix(args), "/usr/bin/rsync"]),
            "-e",
            shlex.join(ssh(args)[:-1]),
            f"{args.ssh_target}:{source}/",
            str(destination) + "/",
        ]
    )
    assert_lease()


def snapshot_protected(args, stage, local, identifier, receipt, expected_sessions):
    protected.require_encrypted_storage(local)
    command = [
        args.remote_python,
        str(protected_script(args)),
        "snapshot",
        "--output-dir",
        str(stage),
        "--generation-id",
        identifier,
        "--data-manifest-sha256",
        receipt["generation_manifest_sha256"],
        "--env-file",
        str(args.protected_env_file),
        "--caddy-file",
        str(args.protected_caddy_file),
    ]
    for value in args.protected_session:
        command += ["--session", value]
    in_phase("create_protected_snapshot", remote, args, command)
    in_phase("transfer_protected_snapshot", transfer_protected, args, stage, local)
    proof = in_phase(
        "verify_protected_snapshot",
        protected.verify,
        local,
        generation_id=identifier,
        data_manifest_sha256=receipt["generation_manifest_sha256"],
        data_created_at=receipt["created_at"],
        expected_sessions=expected_sessions,
    )
    assert_lease()
    return proof


def publish_health(args, data_stage, protected_stage, receipt):
    assert_lease()
    command = [
        args.remote_python,
        str(protected_script(args)),
        "publish",
        "--project-root",
        str(args.remote_root),
        "--source-snapshot-dir",
        str(data_stage),
        "--protected-snapshot-dir",
        str(protected_stage),
        "--service-user",
        getattr(args, "health_service_user", "otz"),
    ]
    result = run(
        [*ssh(args), shlex.join([*prefix(args), *command])],
        input_text=json.dumps(receipt, sort_keys=True),
        timeout=120,
    )
    response = json.loads(result.stdout)
    recovery.require(
        response.get("result") == "health_receipt_published"
        and response.get("generation_id") == receipt["generation_id"],
        "health receipt publication not confirmed",
    )
    assert_lease()


def backup(args):
    with backup_phase("backup_preflight"):
        return _backup(args)


def _backup(args):
    prepared_stage, prepared_checksum = prepared_snapshot_options(args)
    root = args.private_root.resolve()
    # A scope marker is written by the sanitized or data-only driver. Refuse before creating
    # anything, so this driver's rotation can never delete generations of another driver.
    recovery.require(not (root / "scope.json").exists(), "foreign_namespace_marker")
    if getattr(args, "application_project_root", None) is not None:
        recovery.ROOT = args.application_project_root.resolve()
    recovery.private_dir(root)
    protected_enabled, expected_sessions = protected_configuration(args)
    for name in ("generations", "pending", "receipts", "logs"):
        recovery.private_dir(root / name)
    if protected_enabled:
        protected.require_encrypted_storage(root)
        for name in ("protected", "protected-pending"):
            recovery.private_dir(root / name)
    with (root / "backup.lock").open("a+") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"result": "skipped_locked"}
        accepted = completed(root)
        now = datetime.now(UTC)
        last_protected_good = not protected_enabled
        if accepted and protected_enabled:
            last = accepted[-1][2]
            try:
                protected.verify(
                    root / "protected" / accepted[-1][1].name,
                    generation_id=last["generation_id"],
                    data_manifest_sha256=last["generation_manifest_sha256"],
                    data_created_at=last["created_at"],
                    expected_sessions=expected_sessions,
                )
                last_protected_good = last.get("protected_recovery_verified") is True
            except (recovery.RecoveryError, OSError, ValueError, KeyError, TypeError):
                last_protected_good = False
        if (
            accepted
            and prepared_stage is None
            and last_protected_good
            and (
                not getattr(args, "publish_health_receipt", False)
                or accepted[-1][2].get("health_receipt_published") is True
            )
            and accepted[-1][2].get("database", {}).get("revisions") == recovery.schema_heads()
            and 0 <= (now - accepted[-1][0]).total_seconds() < args.interval_hours * 3600
        ):
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
        stage = prepared_stage or args.remote_root / "data/backups" / f".offhost-{uuid4().hex}"
        secret_stage = args.remote_root / "data/backups" / f".protected-{uuid4().hex}"
        secret_args = protected_arguments(args)
        secret_local = root / "protected-pending" / identifier
        if protected_enabled:
            secret_local.mkdir(mode=0o700)
        with ExitStack() as lease_stack:
            in_phase("remote_source_lease", lease_stack.enter_context, remote_lease(args))
            receipt = None
            receipt_published = False
            try:
                command = [
                    args.remote_python,
                    str(
                        getattr(args, "remote_recovery_script", None)
                        or args.remote_root / "scripts/recovery.py"
                    ),
                    "snapshot",
                    "--output-dir",
                    str(stage),
                    "--project-root",
                    str(args.remote_root),
                    "--application-project-root",
                    str(args.remote_root),
                    "--db",
                    str(args.remote_root / "data/otz.db"),
                ]
                for role in ("state", "inbound", "listings"):
                    command += [f"--{role}-dir", str(args.remote_root / "data" / role)]
                command += ["--published-dir", str(args.remote_published_dir)]
                if prepared_stage is not None:
                    in_phase(
                        "check_prepared_snapshot",
                        check_prepared_snapshot,
                        args,
                        stage,
                        prepared_checksum,
                    )
                else:
                    in_phase("create_source_snapshot", remote, args, command)
                in_phase("transfer_data", transfer, args, stage, pending, seed.resolve())
                if prepared_stage is not None:
                    recovery.require(
                        recovery.digest(pending / "manifest.json") == prepared_checksum,
                        "prepared snapshot manifest changed during transfer",
                    )
                assert_lease()
                manifest = in_phase(
                    "verify_data_manifest",
                    recovery.verify_directory,
                    pending,
                    progress_check=assert_lease,
                )
                receipt = in_phase(
                    "verify_application_restore",
                    recovery.verify_application,
                    pending,
                    manifest,
                    run_child=guarded_child,
                )
                assert_lease()
                recovery.require(
                    datetime.fromisoformat(receipt["created_at"]) <= datetime.now(UTC),
                    "source snapshot time is in the future",
                )
                if protected_enabled:
                    receipt["protected_proof"] = in_phase(
                        "protected_recovery",
                        snapshot_protected,
                        secret_args,
                        secret_stage,
                        secret_local,
                        identifier,
                        receipt,
                        expected_sessions,
                    )
                # A combined receipt is true only after this generation's fresh protected checks.
                receipt.update(
                    backup_format="directory_generation",
                    manifest_sha256=receipt["generation_manifest_sha256"],
                    offhost_verified=True,
                    generation_id=identifier,
                    protected_recovery_verified=protected_enabled,
                )
                destination = root / "generations" / identifier
                assert_lease()
                with backup_phase("promote_verified_generation"):
                    pending.rename(destination)
                    if protected_enabled:
                        secret_local.rename(root / "protected" / identifier)
                    atomic_json(root / "receipts" / f"{identifier}.json", receipt)
                receipt_published = True
                if getattr(args, "publish_health_receipt", False):
                    try:
                        in_phase(
                            "publish_health_receipt",
                            publish_health,
                            secret_args,
                            stage,
                            secret_stage,
                            receipt,
                        )
                    except BaseException:
                        receipt["health_receipt_published"] = False
                        atomic_json(root / "receipts" / f"{identifier}.json", receipt)
                        raise
                    receipt["health_receipt_published"] = True
                    atomic_json(root / "receipts" / f"{identifier}.json", receipt)
                # Only accepted generations participate; failed/pending work never displaces one.
                assert_lease()
                with backup_phase("rotate_verified_generations"):
                    prior = [entry for entry in completed(root) if entry[1] != destination]
                    for _, old, _receipt in prior[:-1]:
                        shutil.rmtree(old)
                        if protected_enabled and (root / "protected" / old.name).exists():
                            shutil.rmtree(root / "protected" / old.name)
                        (root / "receipts" / f"{old.name}.json").unlink()
                return receipt
            finally:
                # Generated staging only; never delete original configuration/session/media.
                cleanup = [] if prepared_stage is not None else [(args, stage)]
                if protected_enabled:
                    cleanup.append((secret_args, secret_stage))
                for cleanup_args, cleanup_stage in cleanup:
                    try:
                        remote(
                            cleanup_args,
                            [
                                args.remote_python,
                                "-c",
                                "import shutil,sys; shutil.rmtree(sys.argv[1],ignore_errors=True)",
                                str(cleanup_stage),
                            ],
                        )
                    except (OSError, subprocess.SubprocessError, recovery.RecoveryError):
                        if receipt_published:
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
    parser.add_argument(
        "--prepared-snapshot-dir",
        type=Path,
        help="one-time reuse of a completed manual source snapshot; preserve staging",
    )
    parser.add_argument("--prepared-snapshot-manifest-sha256")
    parser.add_argument(
        "--remote-recovery-script",
        type=Path,
        help="staged recovery helper outside production runtime",
    )
    parser.add_argument(
        "--application-project-root",
        type=Path,
        help="local checkout compatible with the source schema",
    )
    parser.add_argument("--protected-env-file", type=Path)
    parser.add_argument("--protected-caddy-file", type=Path)
    parser.add_argument(
        "--protected-session", action="append", help="explicit alias=/absolute/session.path"
    )
    parser.add_argument(
        "--protected-remote-user", help="explicit sudo user for protected snapshots"
    )
    parser.add_argument("--remote-protected-recovery-script", type=Path)
    parser.add_argument(
        "--publish-health-receipt",
        action="store_true",
        help="atomically publish only a fresh combined proof",
    )
    parser.add_argument("--interval-hours", type=float, default=24)
    parser.add_argument("--min-free-gib", type=float, default=20)
    args = parser.parse_args()
    recovery.require(
        args.remote_root == Path("/opt/otz") and args.private_root.is_absolute(),
        "canonical /opt/otz remote root and absolute private root required",
    )
    safe_remote_path(args.remote_published_dir, args.remote_root / "data")
    prepared_stage, _prepared_checksum = prepared_snapshot_options(args)
    recovery.require(
        args.command != "launch-agent" or prepared_stage is None,
        "prepared snapshots cannot be reused by a scheduled LaunchAgent",
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
        ) as error:
            print(
                json.dumps(
                    {"result": "backup_failed", "accepted": False, **failure_details(error)}
                ),
                file=sys.stderr,
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
