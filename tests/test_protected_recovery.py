"""Verify fresh protected bytes without reading or emitting any real credentials."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import protected_recovery as protected
from scripts import recovery


@pytest.fixture
def protected_source(tmp_path, monkeypatch):
    root = tmp_path / "inputs"
    root.mkdir(mode=0o700)
    env = root / ".env"
    caddy = root / "Caddyfile"
    env.write_text("TOKEN=fixture-secret-value\n")
    caddy.write_text("example.invalid { respond ok }\n")
    sessions = []
    for name in ("userbot", "bot"):
        path = root / f"{name}.session"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE sessions (dc_id INTEGER, auth_key BLOB)")
            db.execute("INSERT INTO sessions VALUES (1, ?)", (b"fixture-authorization-material",))
        sessions.append(f"{name}={path}")
    monkeypatch.setattr(protected, "encrypted_storage", lambda _path: True)
    return argparse.Namespace(
        output_dir=tmp_path / "protected",
        generation_id="generation-1",
        data_manifest_sha256="a" * 64,
        env_file=env,
        caddy_file=caddy,
        session=sessions,
    )


def proof(args):
    return protected.verify(
        args.output_dir,
        generation_id=args.generation_id,
        data_manifest_sha256=args.data_manifest_sha256,
        data_created_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        expected_sessions=["userbot", "bot"],
    )


def test_fresh_online_wal_sessions_and_private_config_produce_sanitized_proof(protected_source):
    args = protected_source
    session_path = Path(args.session[0].partition("=")[2])
    writer = sqlite3.connect(session_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE sessions SET dc_id=5")
    writer.commit()
    assert Path(str(session_path) + "-wal").stat().st_size > 0
    staged = protected.snapshot(args)
    assert staged["result"] == "protected_snapshot_staged_not_verified"
    with sqlite3.connect(args.output_dir / "sessions/userbot.sqlite3") as db:
        assert db.execute("SELECT dc_id FROM sessions").fetchone()[0] == 5
    result = proof(args)
    assert result["protected_recovery_verified"] and result["sessions_verified"] == 2
    assert result["encrypted_storage_verified"] and not result["live_authentication_checked"]
    assert "fixture" not in json.dumps(result) and "TOKEN" not in json.dumps(result)
    assert args.output_dir.stat().st_mode & 0o777 == 0o700
    writer.close()


@pytest.mark.parametrize(
    "damage", ["hash", "permissions", "missing", "symlink", "auth", "generation", "stale"]
)
def test_incomplete_unsafe_old_or_wrong_generation_proof_fails(protected_source, damage):
    args = protected_source
    protected.snapshot(args)
    manifest_file = args.output_dir / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    env = args.output_dir / "config/env"
    if damage == "hash":
        env.write_text("TOKEN=different-fixture\n")
    elif damage == "permissions":
        env.chmod(0o644)
    elif damage == "missing":
        (args.output_dir / "sessions/bot.sqlite3").unlink()
    elif damage == "symlink":
        (args.output_dir / "link").symlink_to(env)
    elif damage == "auth":
        session = args.output_dir / "sessions/bot.sqlite3"
        with sqlite3.connect(session) as db:
            db.execute("UPDATE sessions SET auth_key=NULL")
        manifest["files"]["sessions/bot.sqlite3"]["sha256"] = recovery.digest(session)
        manifest_file.write_text(json.dumps(manifest))
    elif damage == "generation":
        manifest["generation_id"] = "old-generation"
        manifest_file.write_text(json.dumps(manifest))
    else:
        manifest["created_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        manifest_file.write_text(json.dumps(manifest))
    with pytest.raises(recovery.RecoveryError):
        proof(args)


def test_encryption_must_be_verified_and_source_check_never_claims_offhost_proof(
    protected_source, monkeypatch
):
    args = protected_source
    protected.snapshot(args)
    monkeypatch.setattr(protected, "encrypted_storage", lambda _path: False)
    with pytest.raises(recovery.RecoveryError, match="FileVault"):
        proof(args)
    local = protected.verify(
        args.output_dir,
        generation_id=args.generation_id,
        data_manifest_sha256=args.data_manifest_sha256,
        data_created_at=datetime.now(UTC).isoformat(),
        expected_sessions=["userbot", "bot"],
        check_encryption=False,
    )
    assert not local["protected_recovery_verified"]


def test_stale_protected_snapshot_cannot_be_relabelled_as_fresh_data_generation(protected_source):
    args = protected_source
    protected.snapshot(args)
    manifest = args.output_dir / "manifest.json"
    value = json.loads(manifest.read_text())
    value["created_at"] = (datetime.now(UTC) - timedelta(hours=20)).isoformat()
    manifest.write_text(json.dumps(value))
    with pytest.raises(recovery.RecoveryError, match="predates"):
        proof(args)


def test_snapshot_cli_never_prints_secret_value_on_success_or_failure(protected_source):
    args = protected_source
    command = [
        sys.executable,
        "scripts/protected_recovery.py",
        "snapshot",
        "--output-dir",
        str(args.output_dir),
        "--generation-id",
        args.generation_id,
        "--data-manifest-sha256",
        args.data_manifest_sha256,
        "--env-file",
        str(args.env_file),
        "--caddy-file",
        str(args.caddy_file),
    ]
    for session in args.session:
        command += ["--session", session]
    passed = subprocess.run(command, capture_output=True, text=True)
    assert passed.returncode == 0
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode != 0
    assert "fixture-secret" not in passed.stdout + passed.stderr + failed.stdout + failed.stderr
    assert (
        "fixture-authorization" not in passed.stdout + passed.stderr + failed.stdout + failed.stderr
    )


def publication_fixture(args, tmp_path):
    root = tmp_path / "application"
    data_stage = root / "data/backups/data-stage"
    data_stage.mkdir(parents=True)
    (root / "data/state").mkdir()
    stamp = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    data = {"created_at": stamp, "database": {"revisions": ["fixture_schema"]}}
    manifest = data_stage / "manifest.json"
    manifest.write_text(json.dumps(data))
    args.output_dir = root / "data/backups/protected-stage"
    args.data_manifest_sha256 = recovery.digest(manifest)
    protected.snapshot(args)
    receipt = {
        "result": "data_restore_verified",
        "backup_format": "directory_generation",
        "generation_id": args.generation_id,
        "offhost_verified": True,
        "created_at": stamp,
        "database": data["database"],
        "manifest_sha256": args.data_manifest_sha256,
        "generation_manifest_sha256": args.data_manifest_sha256,
        "application": {"catalog": 200, "apartment": 200, "photo": 200, "photo_decoded": True},
        "protected_recovery_verified": True,
        "protected_proof": proof(args),
    }
    with sqlite3.connect(root / "data/otz.db") as db:
        db.execute("CREATE TABLE alembic_version(version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('fixture_schema')")
    publish_args = argparse.Namespace(
        project_root=root,
        source_snapshot_dir=data_stage,
        protected_snapshot_dir=args.output_dir,
        service_user=pwd.getpwuid(os.getuid()).pw_name,
    )
    return publish_args, receipt


def test_publish_checks_current_source_proofs_and_writes_only_private_fixed_health_receipt(
    protected_source, tmp_path
):
    args, receipt = publication_fixture(protected_source, tmp_path)
    result = protected.publish(args, receipt, canonical_root=args.project_root)
    path = args.project_root / "data/state/recovery_receipt.json"
    assert result == {
        "result": "health_receipt_published",
        "generation_id": receipt["generation_id"],
    }
    assert json.loads(path.read_text()) == receipt
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_uid == os.getuid()
    assert list(path.parent.iterdir()) == [path]
    assert "fixture-secret-value" not in path.read_text()


@pytest.mark.parametrize(
    "damage",
    [
        "data_hash",
        "generation",
        "protected_hash",
        "schema",
        "future",
        "old_proof",
        "not_verified",
        "traversal",
        "symlink",
    ],
)
def test_publish_rejects_false_stale_or_mismatched_proof_before_any_health_write(
    protected_source, tmp_path, damage
):
    args, receipt = publication_fixture(protected_source, tmp_path)
    if damage == "data_hash":
        receipt["manifest_sha256"] = "f" * 64
    elif damage == "generation":
        receipt["generation_id"] = "some-other-generation"
    elif damage == "protected_hash":
        receipt["protected_proof"]["manifest_sha256"] = "f" * 64
    elif damage == "schema":
        with sqlite3.connect(args.project_root / "data/otz.db") as db:
            db.execute("UPDATE alembic_version SET version_num='changed_schema'")
    elif damage == "future":
        receipt["created_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    elif damage == "old_proof":
        receipt["protected_proof"]["verified_at"] = (
            datetime.now(UTC) - timedelta(days=2)
        ).isoformat()
    elif damage == "not_verified":
        receipt["protected_proof"]["encrypted_storage_verified"] = False
    elif damage == "traversal":
        args.source_snapshot_dir = args.project_root / "data/backups/../state"
    else:
        alias = args.project_root / "data/backups/outside"
        alias.symlink_to(tmp_path / "inputs")
        args.source_snapshot_dir = alias
    with pytest.raises(recovery.RecoveryError):
        protected.publish(args, receipt, canonical_root=args.project_root)
    assert not list((args.project_root / "data/state").iterdir())


def test_publish_default_rejects_noncanonical_root(protected_source, tmp_path):
    args, receipt = publication_fixture(protected_source, tmp_path)
    with pytest.raises(recovery.RecoveryError, match="canonical"):
        protected.publish(args, receipt)
