"""Reuse only completed pinned staging; full destination verification still decides acceptance."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import offhost_backup as job
from scripts import recovery
from tests.test_offhost_backup import environment, protected_environment
from tests.test_recovery import source as recovery_source


@pytest.fixture
def source(tmp_path):
    return recovery_source.__wrapped__(tmp_path)


def prepared(source, args, monkeypatch):
    stage = source.project_root / "data/backups/.manual-fixture"
    source.output_dir = stage
    recovery.create_snapshot(source)
    args.prepared_snapshot_dir = stage
    args.prepared_snapshot_manifest_sha256 = recovery.digest(stage / "manifest.json")

    def check(parameters, path, checksum):
        return recovery.prepared_snapshot_metadata(path, checksum, parameters.remote_root)

    monkeypatch.setattr(job, "check_prepared_snapshot", check)
    return stage


def test_prepared_snapshot_reuses_staging_without_source_hash_or_stage_cleanup(
    source, tmp_path, monkeypatch
):
    args = environment(source, tmp_path, monkeypatch)
    stage = prepared(source, args, monkeypatch)
    monkeypatch.setattr(
        recovery, "create_snapshot", lambda *_args: pytest.fail("source snapshot was repeated")
    )
    result = job.backup(args)
    assert result["result"] == "data_restore_verified"
    assert stage.is_dir()
    assert recovery.digest(stage / "manifest.json") == args.prepared_snapshot_manifest_sha256
    local = args.private_root / "generations" / result["generation_id"]
    assert recovery.verify_directory(local)["database"] == result["database"]


def test_prepared_data_still_takes_fresh_protected_proof(source, tmp_path, monkeypatch):
    args = protected_environment(source, tmp_path, monkeypatch)
    stage = prepared(source, args, monkeypatch)
    args.protected_env_file.write_text("TOKEN=new-fixture-after-data-snapshot\n")
    args.publish_health_receipt = True
    publications = []

    def publish(_args, data_stage, protected_stage, receipt):
        job.protected.validate_receipt(
            receipt, data_stage / "manifest.json", protected_stage / "manifest.json"
        )
        publications.append(data_stage)

    monkeypatch.setattr(job, "publish_health", publish)
    result = job.backup(args)
    assert stage.is_dir() and result["protected_recovery_verified"]
    assert result["health_receipt_published"] and publications == [stage]
    assert (
        args.private_root / "protected" / result["generation_id"] / "config/env"
    ).read_text() == "TOKEN=new-fixture-after-data-snapshot\n"
    assert (
        result["protected_proof"]["data_manifest_sha256"] == args.prepared_snapshot_manifest_sha256
    )


@pytest.mark.parametrize("damage", ["digest", "future", "old", "root", "extra", "symlink"])
def test_prepared_metadata_rejects_untrusted_incomplete_or_wrong_snapshot(
    source, tmp_path, monkeypatch, damage
):
    args = environment(source, tmp_path, monkeypatch)
    stage = prepared(source, args, monkeypatch)
    manifest = stage / "manifest.json"
    data = json.loads(manifest.read_text())
    if damage == "digest":
        args.prepared_snapshot_manifest_sha256 = "f" * 64
    elif damage in {"future", "old", "root"}:
        if damage == "root":
            data["source_project_root"] = "/different"
        else:
            data["created_at"] = (
                datetime.now(UTC)
                + (timedelta(days=1) if damage == "future" else -timedelta(days=2))
            ).isoformat()
        manifest.write_text(json.dumps(data))
        args.prepared_snapshot_manifest_sha256 = recovery.digest(manifest)
    elif damage == "extra":
        (stage / "unexpected").write_text("extra fixture")
    else:
        (stage / "alias").symlink_to(stage / "data/otz.db")
    with pytest.raises(recovery.RecoveryError):
        job.backup(args)
    assert stage.is_dir()
    assert not job.completed(args.private_root)


def test_prepared_stage_bytes_still_require_receiver_hash_verification(
    source, tmp_path, monkeypatch
):
    args = environment(source, tmp_path, monkeypatch)
    stage = prepared(source, args, monkeypatch)
    database = stage / "data/otz.db"
    content = bytearray(database.read_bytes())
    content[-1] ^= 1
    database.write_bytes(content)
    with pytest.raises(recovery.RecoveryError, match="checksum"):
        job.backup(args)
    assert stage.is_dir() and not job.completed(args.private_root)


def test_prepared_manifest_swap_during_transfer_is_rejected(source, tmp_path, monkeypatch):
    args = environment(source, tmp_path, monkeypatch)
    stage = prepared(source, args, monkeypatch)
    transfer = job.transfer

    def changed(parameters, remote_stage, pending, seed):
        result = transfer(parameters, remote_stage, pending, seed)
        path = pending / "manifest.json"
        path.write_text(path.read_text() + "\n")
        return result

    monkeypatch.setattr(job, "transfer", changed)
    with pytest.raises(recovery.RecoveryError, match="manifest changed"):
        job.backup(args)
    assert stage.is_dir() and not job.completed(args.private_root)


@pytest.mark.parametrize(
    "path,checksum",
    [
        (Path("/opt/otz/data/backups/.manual-fixture"), None),
        (None, "a" * 64),
        (Path("/opt/otz/data/backups"), "a" * 64),
        (Path("/opt/otz/data/backups/../state/.manual-fixture"), "a" * 64),
        (Path("/opt/otz/data/backups/.protected-fixture"), "a" * 64),
    ],
)
def test_prepared_options_fail_closed_before_any_remote_work(path, checksum):
    from argparse import Namespace

    args = Namespace(
        remote_root=Path("/opt/otz"),
        prepared_snapshot_dir=path,
        prepared_snapshot_manifest_sha256=checksum,
    )
    with pytest.raises(recovery.RecoveryError):
        job.prepared_snapshot_options(args)


def test_staged_metadata_cli_is_read_only_and_does_not_claim_restore(source, tmp_path, monkeypatch):
    args = environment(source, tmp_path, monkeypatch)
    stage = prepared(source, args, monkeypatch)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/recovery.py",
            "check-snapshot-metadata",
            str(stage),
            "--manifest-sha256",
            args.prepared_snapshot_manifest_sha256,
            "--project-root",
            str(source.project_root),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "result": "prepared_metadata_checked_not_restored",
        "manifest_sha256": args.prepared_snapshot_manifest_sha256,
    }
    assert recovery.digest(stage / "manifest.json") == args.prepared_snapshot_manifest_sha256


def test_launchagent_cannot_reuse_prepared_snapshot_forever(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/offhost_backup.py",
            "launch-agent",
            "--ssh-target",
            "fixture",
            "--ssh-key",
            str(tmp_path / "absent-key"),
            "--private-root",
            str(tmp_path / "private"),
            "--remote-root",
            "/opt/otz",
            "--remote-python",
            "/opt/otz/.venv/bin/python",
            "--remote-published-dir",
            "/opt/otz/data/inbound",
            "--prepared-snapshot-dir",
            "/opt/otz/data/backups/.manual-fixture",
            "--prepared-snapshot-manifest-sha256",
            "a" * 64,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "scheduled LaunchAgent" in result.stderr
    assert not list(tmp_path.iterdir())
