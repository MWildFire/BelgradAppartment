"""Real synthetic snapshots and offline restore; SSH and transfer boundaries are faked."""

import argparse
import json
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import data_only_backup as backup
from scripts import data_only_preflight as preflight
from tests.test_recovery import source as recovery_source


def test_failure_details_never_exposes_exception_values():
    assert backup.failure_details(backup.recovery.RecoveryError("synthetic private value")) == {
        "result": "blocked", "reason": "recovery_gate_failed",
    }
    assert backup.failure_details(PermissionError("synthetic private path")) == {
        "result": "blocked", "reason": "permission_denied",
    }


def test_failure_details_names_specific_safety_gate_without_weakening_it():
    assert backup.failure_details(backup.recovery.RecoveryError(
        "credential/session-like file in data inputs; use protected recovery storage",
    ))["reason"] == "sensitive_media_entry"
    assert backup.failure_details(preflight.PreflightError(
        "credential_pattern_in_database",
    ))["reason"] == "credential_pattern_in_database"


@pytest.fixture
def source(tmp_path):
    value = recovery_source.__wrapped__(tmp_path)
    value.min_free_gib = 0
    (value.project_root / "release.json").write_text(json.dumps({"revision": "a" * 40}))
    return value


def test_frozen_snapshot_restores_without_state_or_credentials_and_survives_source_change(source):
    (source.state_dir / "telegram.session").write_text("synthetic credential excluded")
    (source.project_root / ".env").write_text("synthetic credential excluded")
    original_db = backup.recovery.digest(source.db)
    result = backup.create_snapshot(source)
    (source.inbound_dir / "photo.jpg").write_bytes(b"source changed after snapshot")
    receipt = backup.verify_snapshot(source.output_dir, application=True)
    manifest = json.loads((source.output_dir / "manifest.json").read_text())
    assert result["result"] == "data_only_source_snapshot_created"
    assert receipt["result"] == "data_restore_verified"
    assert receipt["application"] == {"catalog": 200, "apartment": 200, "photo": 200,
                                      "photo_decoded": True, "network": "disabled"}
    assert receipt["backup_scope"] == "db_media"
    assert receipt["media_files_verified"] == 1
    assert receipt["protected_recovery_verified"] is False
    assert receipt["source_revision"] == "a" * 40
    assert not any(name.startswith("data/state") for name in manifest["files"])
    assert not (source.output_dir / "data/state").exists()
    assert backup.recovery.digest(source.db) == original_db


def test_credential_gate_blocks_source_packet_before_any_transfer(source):
    with sqlite3.connect(source.db) as db:
        db.execute("CREATE TABLE dangerous (api_key TEXT)")
        db.execute("INSERT INTO dangerous VALUES ('synthetic opaque value')")
    with pytest.raises(preflight.PreflightError, match="populated_credential_field"):
        backup.create_snapshot(source)
    assert not source.output_dir.exists()


@pytest.mark.parametrize("member", [".env", "data/state/run.json", "data/telegram.session",
                                   "data/inbound/secret.jpg", "../otz.db"])
def test_forbidden_manifest_members_rejected_before_transfer(source, member):
    backup.create_snapshot(source)
    manifest = json.loads((source.output_dir / "manifest.json").read_text())
    manifest["files"][member] = {"size": 1, "sha256": "a" * 64}
    with pytest.raises(preflight.PreflightError, match="forbidden_snapshot_member"):
        backup.check_manifest(manifest)


def test_changed_media_or_added_file_never_yields_a_restore_receipt(source):
    backup.create_snapshot(source)
    snapshot = source.output_dir
    photo = snapshot / "data/inbound/photo.jpg"
    photo.write_bytes(photo.read_bytes() + b"unexpected bytes")
    with pytest.raises(preflight.PreflightError, match="snapshot_checksum_differs"):
        backup.verify_snapshot(snapshot)
    (snapshot / "extra.txt").write_text("unexpected")
    with pytest.raises(preflight.PreflightError, match="snapshot_inventory_differs"):
        backup.verify_snapshot(snapshot)


def test_receiver_uses_separate_namespace_and_only_transfers_manifest_allowlist(source, tmp_path, monkeypatch):
    backup.create_snapshot(source)
    stage = source.output_dir
    manifest = json.loads((stage / "manifest.json").read_text())
    manifest["source_project_root"] = "/opt/otz"
    for role, item in manifest["components"].items():
        item["source"] = "/opt/otz/data/" + ("inbound" if role == "published" else role)
    (stage / "manifest.json").write_text(json.dumps(manifest))
    seed = tmp_path / "existing-full-generation"
    shutil.copytree(stage, seed)
    (seed / "data/state").mkdir()
    (seed / "data/state/retain.txt").write_text("retain existing backup")
    key = tmp_path / "fixture-key"
    key.write_text("synthetic; no SSH executed")
    args = argparse.Namespace(
        private_root=tmp_path / "data-only", remote_root=Path("/opt/otz"),
        source_snapshot=Path("/var/tmp/otz-data-only/20261006-090000-123abc45"),
        remote_helper=Path("/tmp/otz-dbmedia-tools-123abc45/scripts/data_only_backup.py"),
        ssh_target="fixture", ssh_key=key, remote_user="otz", remote_python="python",
        seed_dir=seed, min_free_gib=0,
    )
    calls = []

    @contextmanager
    def lease(_args):
        yield

    def transfer(command):
        assert command[0] == "/usr/bin/rsync"
        if "--files-from" in command:
            names = Path(command[command.index("--files-from") + 1]).read_bytes().split(b"\0")
            names = [name.decode() for name in names if name]
            calls.extend(names)
            target = Path(command[-1])
            for name in names:
                (target / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(stage / name, target / name)
        else:
            assert command[-2].endswith("/manifest.json")
            shutil.copyfile(stage / "manifest.json", command[-1])

    monkeypatch.setattr(backup.transport, "remote_lease", lease)
    monkeypatch.setattr(backup.transport, "remote", lambda *args: None)
    monkeypatch.setattr(backup.transport, "run", transfer)
    receipt = backup.receive(args)
    assert set(calls) == {"data/otz.db", "data/photo_manifest.json"}
    assert receipt["offhost_verified"] is True
    assert receipt["media_reused_from_verified_seed"] == 1
    assert receipt["backup_format"] == "data_only_directory_generation"
    assert receipt["protected_recovery_verified"] is False
    assert (seed / "data/state/retain.txt").read_text() == "retain existing backup"
    destination = args.private_root / "generations" / args.source_snapshot.name
    assert not (destination / "data/state").exists()
    assert not (args.private_root / "protected").exists()


def test_source_failure_prevents_even_manifest_egress(tmp_path, monkeypatch):
    key = tmp_path / "fixture-key"
    key.write_text("fixture")
    args = argparse.Namespace(
        private_root=tmp_path / "data-only", remote_root=Path("/opt/otz"),
        source_snapshot=Path("/var/tmp/otz-data-only/20261006-090000-123abc45"),
        remote_helper=Path("/tmp/otz-dbmedia-tools-123abc45/scripts/data_only_backup.py"),
        ssh_target="fixture", ssh_key=key, remote_user="otz", remote_python="python",
        seed_dir=None, min_free_gib=0,
    )

    @contextmanager
    def lease(_args):
        yield

    def reject(*args):
        raise preflight.PreflightError("populated_credential_field")

    def forbidden(*args):
        raise AssertionError("transfer attempted before credential inspection passed")

    monkeypatch.setattr(backup.transport, "remote_lease", lease)
    monkeypatch.setattr(backup.transport, "remote", reject)
    monkeypatch.setattr(backup.transport, "run", forbidden)
    with pytest.raises(preflight.PreflightError, match="populated_credential_field"):
        backup.receive(args)
