#!/usr/bin/env python3
"""Fresh private configuration/session recovery proof; never prints secret contents.

Source snapshots contain secret bytes and must stay outside data-backup generations.
Local verification requires FileVault and exact private permissions. A proof confirms
recoverable bytes and SQLite authorization material, not a live Telegram login.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

if __package__:
    from scripts import recovery
else:
    import recovery

FORMAT = "protected_recovery_v1"
MAX_MANIFEST = 1024 * 1024


def require(value, reason):
    recovery.require(value, reason)


def date(value):
    stamp = datetime.fromisoformat(value)
    require(stamp.tzinfo is not None, "protected timestamp missing timezone")
    return stamp.astimezone(UTC)


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value), "invalid proof hash")
    return value


def generation(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value), "invalid generation"
    )
    return value


def encrypted_storage(path):
    if sys.platform != "darwin":
        return False
    home = Path.home().resolve()
    if not path.resolve().is_relative_to(home) or path.stat().st_dev != home.stat().st_dev:
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/fdesetup", "status"], capture_output=True, text=True, check=False, timeout=10
        )
        return result.returncode == 0 and result.stdout.strip() == "FileVault is On."
    except (OSError, subprocess.SubprocessError):
        return False


def require_encrypted_storage(path):
    require(
        encrypted_storage(path), "FileVault storage verification required before copying secrets"
    )


def private_tree(root):
    require(root.is_dir() and not root.is_symlink(), "invalid protected directory")
    for path in (root, *root.rglob("*")):
        mode = path.lstat().st_mode
        require(not stat.S_ISLNK(mode), "protected symlink rejected")
        if stat.S_ISDIR(mode):
            require(stat.S_IMODE(mode) == 0o700, "protected directories require mode 0700")
        else:
            require(
                stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600,
                "protected files require mode 0600",
            )


def session_integrity(path):
    with recovery.ro_db(path) as db:
        require(
            db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "session integrity failed"
        )
        require(
            not db.execute("PRAGMA foreign_key_check").fetchall(), "session foreign keys failed"
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        require("auth_key" in columns, "session authorization table missing")
        present = db.execute("SELECT count(*) FROM sessions WHERE length(auth_key)>0").fetchone()[0]
        require(present > 0, "session authorization material missing")


def config_integrity(path, name):
    content = path.read_text(encoding="utf-8")
    require(
        bool(content.strip()) and "\0" not in content, "protected configuration empty or invalid"
    )
    if name == "env":
        require(
            any(
                re.match(r"\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=", line)
                for line in content.splitlines()
            ),
            "environment configuration has no assignments",
        )


def session_specs(values):
    result = {}
    for value in values:
        name, separator, path = value.partition("=")
        require(separator and re.fullmatch(r"[A-Za-z0-9_-]{1,50}", name), "invalid session alias")
        require(
            name not in result and Path(path).is_absolute(), "duplicate or relative session path"
        )
        result[name] = Path(path)
    require(result, "at least one explicit session is required")
    return result


def snapshot(args):
    identifier = generation(args.generation_id)
    data_hash = sha(args.data_manifest_sha256)
    root = args.output_dir
    require(
        root.is_absolute() and not root.exists(),
        "protected output must be a new absolute directory",
    )
    recovery.private_dir(root)
    sessions = session_specs(args.session)
    manifest = {
        "format": FORMAT,
        "generation_id": identifier,
        "data_manifest_sha256": data_hash,
        "created_at": datetime.now(UTC).isoformat(),
        "files": {},
    }
    for kind in ("config", "sessions"):
        (root / kind).mkdir(mode=0o700)
    for name, original in (("env", args.env_file), ("caddy", args.caddy_file)):
        require(
            original.is_absolute() and original.is_file() and not original.is_symlink(),
            "protected configuration source unavailable",
        )
        before = original.stat()
        require(0 < before.st_size <= 10 * 1024 * 1024, "protected configuration size invalid")
        target = root / "config" / name
        shutil.copyfile(original, target)
        target.chmod(0o600)
        after = original.stat()
        require(
            (before.st_size, before.st_mtime_ns, before.st_ino)
            == (after.st_size, after.st_mtime_ns, after.st_ino),
            "configuration changed while copying",
        )
        config_integrity(target, name)
        manifest["files"][f"config/{name}"] = {
            "size": target.stat().st_size,
            "sha256": recovery.digest(target),
            "kind": "config",
        }
    for name, original in sessions.items():
        require(original.is_file() and not original.is_symlink(), "session source unavailable")
        target = root / "sessions" / f"{name}.sqlite3"
        with recovery.ro_db(original) as source, sqlite3.connect(target) as destination:
            source.backup(destination, pages=1024, sleep=0.1)
            destination.execute("PRAGMA journal_mode=DELETE")
        target.chmod(0o600)
        session_integrity(target)
        manifest["files"][f"sessions/{name}.sqlite3"] = {
            "size": target.stat().st_size,
            "sha256": recovery.digest(target),
            "kind": "session",
        }
    with (root / "manifest.json").open("w") as stream:
        json.dump(manifest, stream, sort_keys=True)
    (root / "manifest.json").chmod(0o600)
    private_tree(root)
    return {"result": "protected_snapshot_staged_not_verified", "generation_id": identifier}


def verify(
    root,
    *,
    generation_id,
    data_manifest_sha256,
    data_created_at,
    expected_sessions,
    check_encryption=True,
    now=None,
):
    now = now or datetime.now(UTC)
    if check_encryption:
        require_encrypted_storage(root)
    private_tree(root)
    manifest_path = root / "manifest.json"
    require(manifest_path.stat().st_size <= MAX_MANIFEST, "protected manifest oversized")
    manifest = json.loads(manifest_path.read_text())
    require(manifest["format"] == FORMAT, "invalid protected format")
    require(manifest["generation_id"] == generation(generation_id), "protected generation mismatch")
    require(
        manifest["data_manifest_sha256"] == sha(data_manifest_sha256),
        "protected data proof mismatch",
    )
    created = date(manifest["created_at"])
    data_created = date(data_created_at)
    require(
        -timedelta(minutes=5) <= now - created <= timedelta(hours=36),
        "protected snapshot stale or future",
    )
    require(
        created >= data_created - timedelta(minutes=5),
        "protected snapshot predates data generation",
    )
    expected = {"config/env", "config/caddy"} | {
        f"sessions/{name}.sqlite3" for name in expected_sessions
    }
    require(
        expected_sessions and set(manifest["files"]) == expected,
        "protected component inventory mismatch",
    )
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    require(actual == expected | {"manifest.json"}, "protected file inventory mismatch")
    for name, info in manifest["files"].items():
        recovery.safe_relative(name)
        path = root / name
        require(
            path.stat().st_size == info["size"] and recovery.digest(path) == info["sha256"],
            "protected checksum mismatch",
        )
        if name.startswith("sessions/"):
            require(info["kind"] == "session", "protected session kind mismatch")
            session_integrity(path)
        else:
            require(info["kind"] == "config", "protected config kind mismatch")
            config_integrity(path, path.name)
    return {
        "result": "protected_recovery_verified"
        if check_encryption
        else "protected_source_verified",
        "protected_recovery_verified": check_encryption,
        "generation_id": generation_id,
        "data_manifest_sha256": data_manifest_sha256,
        "created_at": created.isoformat(),
        "verified_at": now.isoformat(),
        "manifest_sha256": recovery.digest(manifest_path),
        "encrypted_storage_verified": check_encryption,
        "permissions_verified": True,
        "files_hash_verified": True,
        "config_verified": ["env", "caddy"],
        "sessions_verified": len(expected_sessions),
        "session_authorization_material_present": True,
        "live_authentication_checked": False,
    }


def validate_receipt(receipt, data_manifest, protected_manifest, now=None):
    now = now or datetime.now(UTC)
    require(
        receipt["result"] == "data_restore_verified" and receipt["offhost_verified"] is True,
        "data restore proof missing",
    )
    require(receipt["backup_format"] == "directory_generation", "wrong backup format")
    identifier = generation(receipt["generation_id"])
    data_hash = sha(receipt["manifest_sha256"])
    require(
        data_hash == receipt["generation_manifest_sha256"] == recovery.digest(data_manifest),
        "source data manifest does not match receipt",
    )
    require(data_manifest.stat().st_size <= recovery.MAX_MANIFEST_BYTES, "data manifest oversized")
    require(protected_manifest.stat().st_size <= MAX_MANIFEST, "protected manifest oversized")
    with data_manifest.open() as stream:
        data = json.load(stream)
    created = date(data["created_at"])
    require(
        date(receipt["created_at"]) == created
        and timedelta(0) <= now - created <= timedelta(hours=36),
        "data receipt is stale, future or mismatched",
    )
    require(
        receipt["database"] == data["database"], "receipt database differs from source snapshot"
    )
    app = receipt["application"]
    require(
        all(app.get(name) == 200 for name in ("catalog", "apartment", "photo"))
        and app.get("photo_decoded") is True,
        "application restore proof missing",
    )
    proof = receipt["protected_proof"]
    require(
        proof["result"] == "protected_recovery_verified"
        and receipt["protected_recovery_verified"] is True
        and proof["protected_recovery_verified"] is True,
        "protected restore proof missing",
    )
    require(
        proof["generation_id"] == identifier and proof["data_manifest_sha256"] == data_hash,
        "protected receipt generation mismatch",
    )
    require(
        sha(proof["manifest_sha256"]) == recovery.digest(protected_manifest),
        "protected source manifest mismatch",
    )
    require(
        all(
            proof.get(key) is True
            for key in (
                "encrypted_storage_verified",
                "permissions_verified",
                "files_hash_verified",
                "session_authorization_material_present",
            )
        ),
        "protected verification evidence missing",
    )
    require(
        proof.get("config_verified") == ["env", "caddy"]
        and type(proof.get("sessions_verified")) is int
        and proof["sessions_verified"] > 0,
        "protected component proof missing",
    )
    verified = date(proof["verified_at"])
    protected_created = date(proof["created_at"])
    require(
        -timedelta(minutes=5) <= now - verified <= timedelta(hours=36)
        and protected_created >= created - timedelta(minutes=5)
        and protected_created <= verified + timedelta(minutes=5),
        "protected proof stale or mismatched",
    )
    metadata = json.loads(protected_manifest.read_text())
    require(
        metadata["format"] == FORMAT
        and date(metadata["created_at"]) == protected_created
        and metadata["generation_id"] == identifier
        and metadata["data_manifest_sha256"] == data_hash,
        "protected source metadata mismatch",
    )


def publish(args, receipt, *, canonical_root=Path("/opt/otz")):
    require(args.project_root == canonical_root, "canonical application root required")
    require(
        ".." not in args.source_snapshot_dir.parts
        and ".." not in args.protected_snapshot_dir.parts,
        "unsafe snapshot staging path",
    )
    require(
        args.source_snapshot_dir.is_relative_to(args.project_root / "data/backups")
        and args.protected_snapshot_dir.is_relative_to(args.project_root / "data/backups"),
        "snapshot paths outside backup staging",
    )
    require(
        args.source_snapshot_dir.resolve().is_relative_to(canonical_root / "data/backups")
        and args.protected_snapshot_dir.resolve().is_relative_to(canonical_root / "data/backups"),
        "snapshot symlink escapes backup staging",
    )
    validate_receipt(
        receipt,
        args.source_snapshot_dir / "manifest.json",
        args.protected_snapshot_dir / "manifest.json",
    )
    with recovery.ro_db(args.project_root / "data/otz.db") as db:
        revisions = sorted(row[0] for row in db.execute("SELECT version_num FROM alembic_version"))
    require(
        revisions == sorted(receipt["database"]["revisions"]),
        "live schema changed before receipt publication",
    )
    folder = args.project_root / "data/state"
    require(
        folder.is_dir() and folder.resolve() == canonical_root / "data/state",
        "health state directory unavailable",
    )
    account = pwd.getpwnam(args.service_user)
    temporary = folder / f".recovery-receipt-{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, account.pw_uid, account.pw_gid)
        temporary.replace(folder / "recovery_receipt.json")
    finally:
        temporary.unlink(missing_ok=True)
    return {"result": "health_receipt_published", "generation_id": receipt["generation_id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("snapshot")
    for name in ("output-dir", "env-file", "caddy-file"):
        create.add_argument("--" + name, type=Path, required=True)
    create.add_argument("--generation-id", required=True)
    create.add_argument("--data-manifest-sha256", required=True)
    create.add_argument("--session", action="append", required=True)
    upload = commands.add_parser("publish")
    for name in ("project-root", "source-snapshot-dir", "protected-snapshot-dir"):
        upload.add_argument("--" + name, type=Path, required=True)
    upload.add_argument("--service-user", default="otz")
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            result = snapshot(args)
        else:
            raw = sys.stdin.buffer.read(MAX_MANIFEST + 1)
            require(len(raw) <= MAX_MANIFEST, "receipt oversized")
            result = publish(args, json.loads(raw))
        print(json.dumps(result, sort_keys=True))
    except (recovery.RecoveryError, OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        print('{"result":"protected_recovery_failed"}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
