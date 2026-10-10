"""Synthetic data only; every SSH/rsync boundary is replaced by a local fixture."""

import argparse
import ast
import copy
import errno
import fcntl
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts import recovery
from scripts import sanitized_backup as backup
from scripts import sanitized_contract as policy
from scripts import sanitized_helper_pin as pin
from src.db.models import Listing, Platform
from src.matching.phash import compute_phash_from_path
from tests.test_recovery import source as recovery_source
from tests.test_sanitized_contract import synthetic_rows


@pytest.fixture
def source(tmp_path):
    args = recovery_source.__wrapped__(tmp_path)
    args.min_free_gib = 0
    args.acknowledge_url_loss = True
    (args.project_root / "release.json").write_text(
        json.dumps({"revision": policy.SOURCE_REVISION})
    )
    engine = create_engine(f"sqlite:///{args.db}")
    with Session(engine) as session:
        for identity, urls in synthetic_rows():
            platform = Platform.HALOOGLASI if identity <= 2 else Platform.CETIRI_ZIDA
            external = f"synthetic-{identity}"
            folder = args.listings_dir / platform.value / external
            folder.mkdir(parents=True)
            hashes = []
            for index, url in enumerate(urls):
                path = folder / f"{index:03d}{backup._extension(url)}"
                pixels = np.random.default_rng(identity * 100 + index).integers(
                    0, 256, size=(32, 32, 3), dtype=np.uint8
                )
                Image.fromarray(pixels).save(path)
                hashes.append(compute_phash_from_path(path))
            session.add(
                Listing(
                    id=identity + 1,
                    platform=platform,
                    external_id=external,
                    url="https://example.invalid/synthetic",
                    photo_urls=urls,
                    photo_phashes=hashes,
                    photo_embeddings=np.zeros((len(urls), 512), dtype="<f2").tobytes(),
                    photo_evidence=[],
                )
            )
        session.commit()
    engine.dispose()
    return args


@pytest.fixture
def snapshot(source):
    backup.create_snapshot(source)
    return source.output_dir


def manifest_at(snapshot):
    return backup.read_json(snapshot / "manifest.json")


def change_manifest(snapshot, mutate):
    manifest = manifest_at(snapshot)
    mutate(manifest)
    (snapshot / "manifest.json").write_text(json.dumps(manifest))


def photo(source):
    return source.listings_dir / "HALOOGLASI/synthetic-1/000.jpg"


def test_complete_snapshot_and_real_offline_restore_preserve_every_slot_and_source(source):
    # Excluded synthetic sentinels; the handler may not open these at all.
    (source.project_root / ".env").write_text("excluded synthetic value")
    (source.state_dir / "telegram.session").write_text("excluded synthetic value")
    original = recovery.digest(source.db)
    result = backup.create_snapshot(source)
    stage = source.output_dir
    manifest = manifest_at(stage)
    updates, counts = policy.transform([(i + 1, urls) for i, urls in synthetic_rows()])
    backup._compare_databases(source.db, stage / "data/otz.db", dict(updates))
    before_restore = {p.relative_to(stage): recovery.digest(p) for p in recovery.files_below(stage)}
    # The immutable snapshot remains usable independently of later source media changes.
    photo(source).write_bytes(b"changed after snapshot")
    receipt = backup.verify_snapshot(stage, application=True)
    assert receipt["result"] == "data_restore_verified"
    assert receipt["backup_scope"] == policy.SCOPE
    assert receipt["listing_albums"] == backup.ALBUM_RESULT | {"network": "disabled"}
    assert receipt["application"] == {
        "catalog": 200,
        "apartment": 200,
        "photo": 200,
        "photo_decoded": True,
        "network": "disabled",
    }
    assert receipt["protected_recovery_verified"] is False
    assert receipt["offhost_verified"] is False
    assert result["redaction"] == counts == policy.EXPECTED
    assert len(manifest["files"]) == 150  # DB + generated index + inbound + 147 listing photos.
    assert receipt["media_files_verified"] == 148
    assert recovery.digest(source.db) == original
    assert {
        p.relative_to(stage): recovery.digest(p) for p in recovery.files_below(stage)
    } == before_restore
    assert stage.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o077 == 0 for p in recovery.files_below(stage))
    assert not list(stage.parent.glob(".sanitized-build-*"))
    assert not any(
        n.endswith((".session", ".env", "-wal", "-shm", "-journal")) for n in manifest["files"]
    )
    assert not (stage / "data/state").exists()
    assert backup.scan.inspect_database(stage / "data/otz.db")["result"] == "heuristic_scan_passed"


def test_consistent_snapshot_includes_uncheckpointed_wal_without_writing_source(source):
    with sqlite3.connect(source.db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE listings SET price_eur=777 WHERE id=1")
        writer.commit()
        original = recovery.digest(source.db)
        wal = Path(str(source.db) + "-wal")
        wal_hash = recovery.digest(wal)
        backup.create_snapshot(source)
        assert recovery.digest(source.db) == original and recovery.digest(wal) == wal_hash
        with recovery.ro_db(source.output_dir / "data/otz.db") as restored:
            assert restored.execute("SELECT price_eur FROM listings WHERE id=1").fetchone() == (
                777,
            )
        backup._sidecars(source.output_dir / "data/otz.db")


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("no_ack", "url_loss_not_acknowledged"),
        ("release", "source_revision_changed"),
        ("secret", "credential_pattern_in_database"),
        ("credential_field", "populated_credential_field"),
        ("trigger", "unapproved_database_change"),
        ("photo", "affected_photo_content_changed"),
        ("missing_photo", "affected_album_inventory"),
        ("extra_photo", "affected_album_inventory"),
        ("embeddings", "affected_embeddings"),
        ("nonfinite", "affected_embeddings"),
        ("phash", "affected_photo_hashes"),
        ("evidence", "affected_evidence_manifest"),
        ("marker", "preexisting_marker"),
        ("space", "invalid_space_limit"),
    ],
)
def test_source_failures_publish_nothing_and_leave_source_unchanged(source, fault, reason):
    if fault == "no_ack":
        source.acknowledge_url_loss = False
    elif fault == "release":
        (source.project_root / "release.json").write_text('{"revision":"wrong"}')
    elif fault == "space":
        source.min_free_gib = float("nan")
    elif fault in {"photo", "missing_photo", "extra_photo"}:
        if fault == "photo":
            Image.new("RGB", (30, 20), "green").save(photo(source))
        elif fault == "missing_photo":
            photo(source).unlink()
        else:
            shutil.copyfile(photo(source), photo(source).with_name("020.jpg"))
    else:
        with sqlite3.connect(source.db) as db:
            if fault == "secret":
                db.execute("UPDATE inbound_posts SET raw_text='Bearer synthetic_other_credential'")
            elif fault == "credential_field":
                db.execute("CREATE TABLE synthetic_credentials (api_key TEXT)")
                db.execute("INSERT INTO synthetic_credentials VALUES ('synthetic opaque value')")
            elif fault == "trigger":
                db.execute(
                    "CREATE TRIGGER unexpected AFTER UPDATE OF photo_urls ON listings "
                    "BEGIN UPDATE listings SET price_eur=999 WHERE id=NEW.id; END"
                )
            elif fault == "embeddings":
                db.execute("UPDATE listings SET photo_embeddings=? WHERE id=2", (b"short",))
            elif fault == "nonfinite":
                vector = np.zeros((20, 512), dtype="<f2")
                vector[0, 0] = float("nan")
                db.execute("UPDATE listings SET photo_embeddings=? WHERE id=2", (vector.tobytes(),))
            elif fault == "phash":
                db.execute("UPDATE listings SET photo_phashes='[]' WHERE id=2")
            elif fault == "evidence":
                db.execute("UPDATE listings SET photo_evidence='[{}]' WHERE id=2")
            else:
                db.execute(
                    "UPDATE listings SET photo_urls='[\"restore-local:///000.jpg\"]' WHERE id=1"
                )
    original = recovery.digest(source.db)
    with pytest.raises(backup.scan.PreflightError, match=reason):
        backup.create_snapshot(source)
    assert recovery.digest(source.db) == original
    assert not source.output_dir.exists()
    assert not list(source.output_dir.parent.glob(".sanitized-build-*"))


def test_source_symlink_rejected_without_following_it(source, tmp_path):
    target = tmp_path / "outside.jpg"
    photo(source).rename(target)
    photo(source).symlink_to(target)
    with pytest.raises(recovery.RecoveryError, match="symlinks are not allowed"):
        backup.create_snapshot(source)
    assert not source.output_dir.exists()


def test_photo_changes_between_validation_and_copy_are_rejected(source, monkeypatch):
    inspect = backup.scan.inspect_image
    calls = 0

    def inspect_then_change(path):
        nonlocal calls
        result = inspect(path)
        if path == photo(source):
            calls += 1
            if calls == 2:
                path.write_bytes(path.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(backup.scan, "inspect_image", inspect_then_change)
    with pytest.raises(backup.scan.PreflightError, match="media_changed"):
        backup.create_snapshot(source)
    assert not source.output_dir.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m.update(format="renfinder-db-media-v1"),
        lambda m: m.update(scope="db_media"),
        lambda m: m.update(extra="unexpected"),
        lambda m: m.update(protected_recovery_verified=True),
        lambda m: m.update(source_snapshot_immutable=False),
        lambda m: m["sanitization"].update(original_urls_retained=True),
        lambda m: m["sanitization"]["credential_inspection"].update(
            scanner_exemptions=["photo_urls"]
        ),
        lambda m: m["database"].update(extra="unexpected"),
        lambda m: m["database"].update(revisions=["obsolete"]),
        lambda m: m["database"]["table_counts"].update(listings=True),
        lambda m: m["database"].update(schema_sha256="malformed"),
        lambda m: m.update(source_project_root="/opt/../otz"),
        lambda m: m.update(source_project_root="/opt//otz"),
        lambda m: m["components"].pop("inbound"),
        lambda m: m["affected_albums"].update(listings=True),
        lambda m: m.update(photo_references=True),
        lambda m: m["credential_inspection"].update(arbitrary_secrets_absence_proven=True),
        lambda m: m["files"].update({"data/.env": {"size": 1, "sha256": "a" * 64}}),
        lambda m: m["files"]["data/otz.db"].update(size=True),
        lambda m: m["files"]["data/otz.db"].update(extra="unexpected"),
        lambda m: m.update(created_at=(datetime.now(UTC) - timedelta(days=2)).isoformat()),
        lambda m: m.update(created_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat()),
        lambda m: m.update(created_at="yesterday"),
    ],
)
def test_strict_manifest_rejects_scope_and_nested_metadata_drift(snapshot, mutation):
    manifest = manifest_at(snapshot)
    mutation(manifest)
    with pytest.raises(backup.scan.PreflightError):
        backup.check_manifest(manifest)


@pytest.mark.parametrize("fault", ["checksum", "extra_file", "sidecar", "duplicate_json", "index"])
def test_frozen_snapshot_failures_never_yield_a_receipt(snapshot, fault):
    if fault == "checksum":
        (snapshot / "data/inbound/photo.jpg").write_bytes(b"damaged")
    elif fault == "extra_file":
        (snapshot / "extra.txt").write_text("unexpected")
    elif fault == "sidecar":
        (snapshot / "data/otz.db-wal").write_bytes(b"")
    elif fault == "duplicate_json":
        path = snapshot / "manifest.json"
        path.write_text('{"format":"wrong",' + path.read_text()[1:])
    else:
        index = snapshot / "data/photo_manifest.json"
        index.write_text('{"unexpected":{"w":1,"h":1}}')
        change_manifest(
            snapshot,
            lambda m: m["files"].update(
                {
                    "data/photo_manifest.json": {
                        "size": index.stat().st_size,
                        "sha256": recovery.digest(index),
                    }
                }
            ),
        )
    with pytest.raises(backup.scan.PreflightError):
        backup.verify_snapshot(snapshot)


REAL_FCNTL = fcntl.fcntl  # the real call, captured before any test patches it


@pytest.fixture(autouse=True)
def copy_volume_accepts_full_flush(monkeypatch):
    """Model a volume that accepts F_FULLFSYNC, so the copy path runs on hosts without it (Linux CI).

    Refusal and absence of the call are tested explicitly, where they matter.
    """
    monkeypatch.setattr(backup.fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(backup.fcntl, "fcntl", lambda descriptor, command: os.fsync(descriptor) or 0)


def receiver_args(tmp_path):
    key = tmp_path / "fixture-key"
    key.write_text("synthetic only; no SSH executed")
    return argparse.Namespace(
        private_root=tmp_path / "data-only/sanitized-v1",
        remote_root=Path("/opt/otz"),
        source_snapshot=Path("/var/tmp/otz-data-only-sanitized/20261006-090000-123abc45"),
        remote_helper=Path("/tmp/otz-sanitized-tools-123abc45/scripts/sanitized_backup.py"),
        ssh_target="fixture",
        ssh_key=key,
        remote_user="otz",
        remote_python="/opt/otz/.venv/bin/python",
        min_free_gib=0,
        acknowledge_url_loss=True,
    )


@contextmanager
def fake_lease(_args):
    yield


def fake_transport(snapshot, args, monkeypatch, fault=None):
    def remote_manifest(m):
        m["source_project_root"] = "/opt/otz"
        for role, item in m["components"].items():
            item["source"] = "/opt/otz/data/" + ("inbound" if role == "published" else role)

    change_manifest(snapshot, remote_manifest)
    expected_hash = recovery.digest(snapshot / "manifest.json")
    calls = []

    def source(_args, command):
        # Every source helper run is the pinned bootstrap, never a bare helper path.
        assert command[1:4] == ["-I", "-c", pin.HELPER_BOOT]
        if "inspect-snapshot" in command:
            return SimpleNamespace(
                stdout=json.dumps({"result": "snapshot_absent", "scope": policy.SCOPE})
            )
        if "discard-snapshot" in command:
            assert command[command.index("--manifest-sha256") + 1] == expected_hash
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "result": "sanitized_source_snapshot_removed",
                        "scope": policy.SCOPE,
                    }
                )
            )
        assert "snapshot" in command and "--acknowledge-url-loss" in command
        if fault == "source":
            raise backup.scan.PreflightError("credential_pattern_in_database")
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "result": "sanitized_source_snapshot_created",
                    "scope": policy.SCOPE,
                    "manifest_sha256": expected_hash,
                }
            )
        )

    def transfer(command):
        assert command[0] == "/usr/bin/rsync"
        assert args.ssh_target not in command[command.index("-e") + 1].split()
        assert "--link-dest" not in command and "--delete" not in command
        calls.append(command)
        if "--files-from" in command:
            names = Path(command[command.index("--files-from") + 1]).read_bytes().split(b"\0")
            names = [n.decode() for n in names if n]
            assert set(names) <= set(manifest_at(snapshot)["files"])
            for name in names:
                target = Path(command[-1]) / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(snapshot / name, target)
            if fault == "media":
                (Path(command[-1]) / "data/inbound/photo.jpg").write_bytes(b"changed")
        else:
            assert command[-2].endswith("/manifest.json")
            shutil.copyfile(snapshot / "manifest.json", command[-1])
            if fault == "manifest":
                Path(command[-1]).write_text("{}")

    monkeypatch.setattr(backup.transport, "remote_lease", fake_lease)
    monkeypatch.setattr(backup.transport, "remote", source)
    monkeypatch.setattr(backup, "_snapshot_remote", source)
    monkeypatch.setattr(backup.transport, "run", transfer)
    return calls


@pytest.mark.parametrize("remote_user", ["otz"])
def test_separate_receiver_and_real_offline_restore_leave_ordinary_generations_untouched(
    snapshot, tmp_path, monkeypatch, remote_user
):
    args = receiver_args(tmp_path)
    args.remote_user = remote_user
    parent = args.private_root.parent
    parent.mkdir()
    old = parent / "generations/older/protected"
    old.mkdir(parents=True)
    sentinel = old / "synthetic-retain.txt"
    sentinel.write_text("never read or change ordinary generation")
    (parent / "scope.json").write_text('{"scope":"db_media"}')
    calls = fake_transport(snapshot, args, monkeypatch)
    result = backup.receive(args)
    assert len(calls) == 2
    assert result["offhost_verified"] is True
    assert result["protected_recovery_verified"] is False
    assert result["backup_scope"] == policy.SCOPE
    assert result["backup_format"] == "sanitized_directory_generation"
    assert result["media_reused_from_verified_seed"] == 0
    assert result["files_transferred"] == 150
    assert result["source_temporary_snapshot_removed"] is True
    assert sentinel.read_text() == "never read or change ordinary generation"
    assert (parent / "scope.json").read_text() == '{"scope":"db_media"}'
    receipt = args.private_root / "receipts" / (args.source_snapshot.name + ".json")
    assert backup.read_json(receipt) == result
    assert not list((args.private_root / "pending").iterdir())
    with pytest.raises(backup.scan.PreflightError, match="generation_exists"):
        backup.receive(args)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "fault,transfers",
    [
        ("source", 0),
        ("manifest", 1),
        ("media", 2),
    ],
)
def test_receiver_failures_cannot_publish_receipts_or_generations(
    snapshot, tmp_path, monkeypatch, fault, transfers
):
    args = receiver_args(tmp_path)
    calls = fake_transport(snapshot, args, monkeypatch, fault)
    with pytest.raises(backup.scan.PreflightError):
        backup.receive(args)
    assert len(calls) == transfers
    assert not list((args.private_root / "pending").iterdir())
    assert not list((args.private_root / "receipts").iterdir())
    assert not list((args.private_root / "generations").iterdir())


@pytest.mark.parametrize(
    "fault", ["no_ack", "namespace", "source_path", "helper", "binding", "orphan"]
)
def test_receiver_guards_precede_any_transport(tmp_path, monkeypatch, fault):
    args = receiver_args(tmp_path)
    if fault == "no_ack":
        args.acknowledge_url_loss = False
    elif fault == "namespace":
        args.private_root = tmp_path / "data-only"
    elif fault == "source_path":
        args.source_snapshot = Path("/var/tmp/otz-data-only/ordinary")
    elif fault == "helper":
        args.remote_helper = Path("/tmp/unrelated.py")
    elif fault == "binding":
        args.private_root.mkdir(parents=True, mode=0o700)
        (args.private_root / "scope.json").write_text('{"scope":"db_media"}')
    else:
        (args.private_root / "receipts").mkdir(parents=True, mode=0o700)
        args.private_root.chmod(0o700)
        (args.private_root / "receipts/orphan.json").write_text("{}")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("transport before local safety gate")

    monkeypatch.setattr(backup.transport, "remote_lease", forbidden)
    with pytest.raises(backup.scan.PreflightError):
        backup.receive(args)


def test_receive_transfers_in_bounded_batches(snapshot, tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    calls = fake_transport(snapshot, args, monkeypatch)
    from scripts import bounded_transfer

    real_batches = bounded_transfer.batches
    monkeypatch.setattr(
        bounded_transfer, "batches", lambda names: real_batches(names, max_files=40)
    )
    result = backup.receive(args)
    listings = [call for call in calls if "--files-from" in call]
    assert len(listings) == 4  # 150 synthetic files at 40 names per rsync call
    assert result["files_transferred"] == 150
    assert result["offhost_verified"] is True


def test_receive_keeps_verified_copy_when_lease_fails_after_source_discard(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    discard = backup._discard_remote
    discarded = []

    def discard_then_flag(remote_args, checksum):
        discard(remote_args, checksum)
        discarded.append(checksum)

    def lease_lost():
        if discarded:
            raise backup.scan.PreflightError("lease_lost_after_discard")

    monkeypatch.setattr(backup, "_discard_remote", discard_then_flag)
    monkeypatch.setattr(backup.transport, "assert_lease", lease_lost)
    with pytest.raises(backup.scan.PreflightError, match=r"^lease_lost_after_discard$"):
        backup.receive(args)
    generation = args.source_snapshot.name
    assert (args.private_root / "pending" / generation / "manifest.json").exists()
    record = backup.read_json(args.private_root / "resume" / (generation + ".json"))
    assert record["state"] == "kept_for_operator"
    for name in ("receipts", "generations"):
        assert not list((args.private_root / name).iterdir())


def test_receive_failure_keeps_remote_snapshot_and_records_resume(snapshot, tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch, "manifest")
    discards = []
    remote = backup.transport.remote

    def recording(remote_args, command):
        if "discard-snapshot" in command:
            discards.append(command)
        return remote(remote_args, command)

    monkeypatch.setattr(backup.transport, "remote", recording)
    with pytest.raises(backup.scan.PreflightError, match=r"^source_manifest_changed$"):
        backup.receive(args)
    assert discards == []
    record = args.private_root / "resume" / (args.source_snapshot.name + ".json")
    written = backup.read_json(record)
    assert backup._valid_request_id(written["request_id"])
    assert written == {
        "format": policy.FORMAT,
        "scope": policy.SCOPE,
        "generation": args.source_snapshot.name,
        "source_snapshot": str(args.source_snapshot),
        "manifest_sha256": recovery.digest(snapshot / "manifest.json"),
        "request_id": written["request_id"],
        "helper_set_sha256": pin.HELPER_SET_SHA256,
        "state": "transferring",
    }
    for name in ("pending", "receipts", "generations"):
        assert not list((args.private_root / name).iterdir())


def test_receive_resume_reuses_retained_snapshot_without_a_second_snapshot(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch, "manifest")
    with pytest.raises(backup.scan.PreflightError):
        backup.receive(args)
    args.resume_manifest_sha256 = recovery.digest(snapshot / "manifest.json")
    calls = fake_transport(snapshot, args, monkeypatch)

    def no_second_snapshot(*_args, **_kwargs):
        raise AssertionError("resume must reuse the retained source snapshot")

    monkeypatch.setattr(backup, "_snapshot_remote", no_second_snapshot)
    result = backup.receive(args)
    assert result["offhost_verified"] is True
    assert result["files_transferred"] == 150
    receipt = args.private_root / "receipts" / (args.source_snapshot.name + ".json")
    assert backup.read_json(receipt) == result
    assert not (args.private_root / "resume" / (args.source_snapshot.name + ".json")).exists()
    assert len([call for call in calls if "--files-from" in call]) == 1


def test_receive_resume_requires_matching_record_before_transport(tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    args.resume_manifest_sha256 = "a" * 64

    def forbidden(*_args, **_kwargs):
        raise AssertionError("transport before resume record check")

    monkeypatch.setattr(backup.transport, "remote_lease", forbidden)
    with pytest.raises(backup.scan.PreflightError, match=r"^resume_record_mismatch$"):
        backup.receive(args)


def test_unconfirmed_discard_keeps_copy_and_refuses_resume(snapshot, tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    discard = backup._discard_remote

    def removed_then_unconfirmed(remote_args, checksum):
        discard(remote_args, checksum)
        shutil.rmtree(snapshot)  # the server snapshot is gone, but its confirmation never arrives
        raise backup.scan.PreflightError("source_snapshot_cleanup_unconfirmed")

    monkeypatch.setattr(backup, "_discard_remote", removed_then_unconfirmed)
    with pytest.raises(backup.scan.PreflightError, match=r"^source_snapshot_cleanup_unconfirmed$"):
        backup.receive(args)
    generation = args.source_snapshot.name
    assert (args.private_root / "pending" / generation / "manifest.json").exists()
    record = backup.read_json(args.private_root / "resume" / (generation + ".json"))
    assert record["state"] == "kept_for_operator"
    for name in ("receipts", "generations"):
        assert not list((args.private_root / name).iterdir())
    args.resume_manifest_sha256 = record["manifest_sha256"]
    with pytest.raises(backup.scan.PreflightError, match=r"^resume_requires_operator$"):
        backup.receive(args)
    assert (args.private_root / "pending" / generation / "manifest.json").exists()


def test_verified_copy_survives_a_lease_loss_before_discard(snapshot, tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    verify = backup.verify_snapshot
    state = {"verified": False}

    def verified_then_flag(*call_args, **call_kwargs):
        result = verify(*call_args, **call_kwargs)
        state["verified"] = True
        return result

    def lease_lost_after_verify():
        if state["verified"]:
            raise backup.scan.PreflightError("lease_lost_after_verify")

    def forbidden_discard(*_args, **_kwargs):
        raise AssertionError("discard must not run after the lease was lost")

    monkeypatch.setattr(backup, "verify_snapshot", verified_then_flag)
    monkeypatch.setattr(backup.transport, "assert_lease", lease_lost_after_verify)
    monkeypatch.setattr(backup, "_discard_remote", forbidden_discard)
    with pytest.raises(backup.scan.PreflightError, match=r"^lease_lost_after_verify$"):
        backup.receive(args)
    generation = args.source_snapshot.name
    assert (args.private_root / "pending" / generation / "manifest.json").exists()
    record = backup.read_json(args.private_root / "resume" / (generation + ".json"))
    assert record["state"] == "verified"
    for name in ("receipts", "generations"):
        assert not list((args.private_root / name).iterdir())


def test_record_write_failure_before_discard_stops_and_keeps_the_verified_copy(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    write = backup._durable_write

    def write_fails_for_kept(path, value):
        if value.get("state") == backup.KEPT_FOR_OPERATOR:
            raise OSError(28, "No space left on device")
        return write(path, value)

    def forbidden_discard(*_args, **_kwargs):
        raise AssertionError("the source must not be discarded without a durable kept record")

    monkeypatch.setattr(backup, "_durable_write", write_fails_for_kept)
    monkeypatch.setattr(backup, "_discard_remote", forbidden_discard)
    with pytest.raises(OSError, match="No space left"):
        backup.receive(args)
    generation = args.source_snapshot.name
    assert (args.private_root / "pending" / generation / "manifest.json").exists()
    record = backup.read_json(args.private_root / "resume" / (generation + ".json"))
    assert record["state"] == backup.VERIFIED


def test_unresolved_resume_record_blocks_a_fresh_run(tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    args.private_root.mkdir(parents=True, mode=0o700)
    (args.private_root / "scope.json").write_text(
        json.dumps({"format": policy.FORMAT, "scope": policy.SCOPE})
    )
    (args.private_root / "resume").mkdir(mode=0o700)
    (args.private_root / "resume" / "20261006-080000-deadbeef.json").write_text("{}")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("transport before the open-record guard")

    monkeypatch.setattr(backup.transport, "remote_lease", forbidden)
    with pytest.raises(backup.scan.PreflightError, match=r"^unresolved_resume_record$"):
        backup.receive(args)


def test_resume_record_is_written_before_the_first_transfer(snapshot, tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    record = args.private_root / "resume" / (args.source_snapshot.name + ".json")
    transfer = backup.transport.run
    seen = []

    def checked(command):
        seen.append(record.exists())
        return transfer(command)

    monkeypatch.setattr(backup.transport, "run", checked)
    result = backup.receive(args)
    assert seen[0] is True
    assert result["offhost_verified"] is True


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("manifest_sha256", "b" * 64, "resume_record_mismatch"),
        ("generation", "20261006-090001-123abc45", "resume_record_mismatch"),
        (
            "source_snapshot",
            "/var/tmp/otz-data-only-sanitized/20261006-090001-123abc45",
            "resume_record_mismatch",
        ),
        ("state", "kept_for_operator", "resume_requires_operator"),
        ("state", "bogus", "resume_record_mismatch"),
    ],
)
def test_resume_refuses_a_tampered_record_before_transport(
    snapshot, tmp_path, monkeypatch, field, value, code
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch, "manifest")
    with pytest.raises(backup.scan.PreflightError):
        backup.receive(args)
    record = args.private_root / "resume" / (args.source_snapshot.name + ".json")
    data = backup.read_json(record)
    data[field] = value
    record.write_text(json.dumps(data))
    args.resume_manifest_sha256 = recovery.digest(snapshot / "manifest.json")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("transport before the resume record check")

    monkeypatch.setattr(backup.transport, "remote_lease", forbidden)
    with pytest.raises(backup.scan.PreflightError, match=rf"^{code}$"):
        backup.receive(args)


def test_application_child_failure_cannot_print_private_stderr_or_pass(snapshot, monkeypatch):
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=1, stdout="", stderr="synthetic private diagnostic"
        ),
    )
    with pytest.raises(backup.scan.PreflightError, match=r"^offline_application_restore_failed$"):
        backup.verify_snapshot(snapshot, application=True)


def test_child_network_guard_blocks_dns_and_connect_before_smoke(snapshot, tmp_path, monkeypatch):
    # Restore globals after testing the child-only guard in this process.
    monkeypatch.setattr(backup.os, "environ", copy.copy(backup.os.environ))
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            monkeypatch.setattr(socket.socket, name, getattr(socket.socket, name))
    for name in (
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "create_connection",
    ):
        monkeypatch.setattr(socket, name, getattr(socket, name))

    def intercept(_folder):
        assert dict(backup.os.environ) == {}
        with pytest.raises(RuntimeError, match="offline_restore_network_disabled"):
            socket.getaddrinfo("fixture.invalid", 443)
        with pytest.raises(RuntimeError, match="offline_restore_network_disabled"):
            socket.gethostbyname("fixture.invalid")
        with socket.socket() as sock:
            with pytest.raises(RuntimeError, match="offline_restore_network_disabled"):
                sock.connect(("127.0.0.1", 443))
            with pytest.raises(RuntimeError, match="offline_restore_network_disabled"):
                sock.connect_ex(("127.0.0.1", 443))
        with (
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
            pytest.raises(RuntimeError, match="offline_restore_network_disabled"),
        ):
            sock.sendto(b"fixture", ("127.0.0.1", 443))
        raise RuntimeError("guard_verified_stop")

    monkeypatch.setattr(backup.recovery, "application_smoke", intercept)
    scratch = tmp_path / ".sanitized-restore-guard"
    scratch.mkdir()
    with pytest.raises(RuntimeError, match="guard_verified_stop"):
        backup._smoke(snapshot, scratch)


def test_cli_errors_are_value_free_and_require_explicit_loss_acknowledgement(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(Path(backup.__file__)),
            "snapshot",
            "--project-root",
            str(tmp_path / "synthetic-private-path"),
            "--output-dir",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1 and not result.stderr
    assert json.loads(result.stdout) == {"result": "blocked", "reason": "url_loss_not_acknowledged"}


# 500,000 entries at realistic name lengths: more than the 415,000 files of a full source copy, and
# larger than the former 64 MiB limit. Run in a fresh process, so the peak memory is this work alone.
MANIFEST_SCALE_CODE = """
import json, resource, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts import offhost_backup as transport
from scripts import sanitized_backup as backup
path = Path(sys.argv[2])
count = 500_000
files = {
    f"data/listings/CETIRI_ZIDA/{index:08d}-synthetic-listing-identifier/{index % 1000:03d}.webp": {
        "size": 123456,
        "sha256": "0" * 64,
    }
    for index in range(count)
}
checked = backup.check_manifest_size({"files": files})
transport.atomic_json(path, {"files": files})
assert len(backup.read_json(path)["files"]) == count
print(json.dumps({
    "entries": count,
    "checked_bytes": checked,
    "file_bytes": path.stat().st_size,
    "peak_maxrss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
}))
"""


def test_manifest_with_500000_entries_is_checked_written_and_read_within_the_limit(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            MANIFEST_SCALE_CODE,
            str(REPOSITORY),
            str((tmp_path / "manifest.json").resolve()),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    measured = json.loads(result.stdout)
    print("manifest scale measurement:", measured)
    assert measured["entries"] == 500_000
    assert 64 * 1024 * 1024 < measured["file_bytes"] <= backup.MAX_MANIFEST_BYTES
    # The build-time check measures the bytes atomic_json writes, not an estimate.
    assert measured["checked_bytes"] == measured["file_bytes"]


def test_build_directory_left_by_a_killed_build_is_removed_under_the_build_lock(source):
    parent = source.output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    parent.chmod(0o700)
    orphan = parent / ".sanitized-build-0badc0de"  # what a SIGKILLed build leaves behind
    orphan.mkdir(mode=0o700)
    (orphan / "partial.db").write_bytes(b"synthetic partial copy")
    backup.create_snapshot(source)
    assert not orphan.exists()
    assert (source.output_dir / "manifest.json").exists()


def test_source_build_refuses_a_manifest_over_the_limit_before_the_full_recheck(source, monkeypatch):
    def recheck_must_not_run(*_args, **_kwargs):
        raise AssertionError("the full recheck ran although the manifest is over the limit")

    monkeypatch.setattr(backup, "MAX_MANIFEST_BYTES", 1024)
    monkeypatch.setattr(backup, "verify_snapshot", recheck_must_not_run)
    with pytest.raises(backup.scan.PreflightError, match=r"^manifest_size_limit$"):
        backup.create_snapshot(source)
    assert not source.output_dir.exists()


def test_exact_photo_evidence_manifest_is_accepted(source):
    with sqlite3.connect(source.db) as db:
        for identity, platform, external, hashes in db.execute(
            "SELECT id,platform,external_id,photo_phashes FROM listings WHERE id>1"
        ).fetchall():
            folder = source.listings_dir / platform / external
            evidence = [
                {"path": path.name, "phash": phash, "sha256": recovery.digest(path)}
                for path, phash in zip(sorted(folder.iterdir()), json.loads(hashes), strict=True)
            ]
            db.execute(
                "UPDATE listings SET photo_evidence=? WHERE id=?", (json.dumps(evidence), identity)
            )
    backup.create_snapshot(source)
    assert backup.verify_snapshot(source.output_dir)["affected_albums"] == backup.ALBUM_RESULT


@pytest.mark.parametrize("identical", [True, False])
def test_legacy_duplicate_hash_requires_identical_bytes(source, identical):
    folder = photo(source).parent
    if identical:
        shutil.copyfile(folder / "000.jpg", folder / "001.jpg")
    else:
        Image.new("RGB", (32, 32), "blue").save(folder / "000.jpg")
        Image.new("RGB", (32, 32), "red").save(folder / "001.jpg")
    with sqlite3.connect(source.db) as db:
        hashes = json.loads(
            db.execute("SELECT photo_phashes FROM listings WHERE id=2").fetchone()[0]
        )
        hashes[:2] = [compute_phash_from_path(folder / name) for name in ("000.jpg", "001.jpg")]
        assert hashes[0] == hashes[1]
        db.execute("UPDATE listings SET photo_phashes=? WHERE id=2", (json.dumps(hashes),))
    if identical:
        backup.create_snapshot(source)
        assert backup.verify_snapshot(source.output_dir)["affected_albums"] == backup.ALBUM_RESULT
    else:
        with pytest.raises(backup.scan.PreflightError, match="ambiguous_legacy_evidence"):
            backup.create_snapshot(source)
        assert not source.output_dir.exists()


def test_affected_album_budget_is_enforced_before_receipt(source, monkeypatch):
    inspect = backup.scan.inspect_image

    def large_metadata(path):
        return inspect(path) | {"size": 2 * 1024 * 1024}

    monkeypatch.setattr(backup.scan, "inspect_image", large_metadata)
    with pytest.raises(backup.scan.PreflightError, match="affected_evidence_budget"):
        backup.create_snapshot(source)


@pytest.mark.parametrize("fault", ["wrong_index", "wrong_extension", "fewer_markers"])
def test_restored_marker_drift_is_detected_even_after_new_file_checksums(snapshot, fault):
    db_path = snapshot / "data/otz.db"
    with sqlite3.connect(db_path) as db:
        urls = json.loads(db.execute("SELECT photo_urls FROM listings WHERE id=2").fetchone()[0])
        urls[0] = {
            "wrong_index": "restore-local:///001.jpg",
            "wrong_extension": "restore-local:///000.webp",
            "fewer_markers": "https://example.invalid/ordinary.jpg",
        }[fault]
        db.execute("UPDATE listings SET photo_urls=? WHERE id=2", (json.dumps(urls),))
        db.commit()
        db.execute("VACUUM")

    def update(m):
        m["files"]["data/otz.db"] = {
            "size": db_path.stat().st_size,
            "sha256": recovery.digest(db_path),
        }
        m["credential_inspection"] = backup.scan.inspect_database(db_path)

    change_manifest(snapshot, update)
    with pytest.raises(backup.scan.PreflightError):
        backup.verify_snapshot(snapshot)


@pytest.mark.parametrize("member", ["data/inbound/photo.jpg", "manifest.json"])
def test_smoke_must_leave_every_snapshot_byte_unchanged(snapshot, monkeypatch, member):
    def child(*_args, **_kwargs):
        (snapshot / member).write_bytes(b"unexpected child write")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "application": {
                        "catalog": 200,
                        "apartment": 200,
                        "photo": 200,
                        "photo_decoded": True,
                        "network": "disabled",
                    },
                    "listing_albums": backup.ALBUM_RESULT | {"network": "disabled"},
                }
            ),
        )

    monkeypatch.setattr(backup.subprocess, "run", child)
    with pytest.raises(backup.scan.PreflightError, match="snapshot_changed_during_restore"):
        backup.verify_snapshot(snapshot, application=True)


@pytest.mark.parametrize("fault", [None, "namespace", "checksum", "extra_file"])
def test_cleanup_is_limited_to_pinned_temporary_generation(snapshot, monkeypatch, fault):
    # Relocate the exact disposable source namespace into pytest's synthetic directory.
    monkeypatch.setattr(backup, "SOURCE_SNAPSHOT", re.compile(re.escape(str(snapshot))))
    manifest = manifest_at(snapshot)
    manifest["source_project_root"] = "/opt/otz"
    for role, item in manifest["components"].items():
        item["source"] = "/opt/otz/data/" + ("inbound" if role == "published" else role)
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    args = SimpleNamespace(
        directory=snapshot, manifest_sha256=recovery.digest(snapshot / "manifest.json")
    )
    if fault == "namespace":
        monkeypatch.setattr(backup, "SOURCE_SNAPSHOT", re.compile(r"/never-allowed"))
    elif fault == "checksum":
        args.manifest_sha256 = "a" * 64
    elif fault == "extra_file":
        (snapshot / "unrelated.txt").write_text("must retain")
    if fault:
        with pytest.raises(backup.scan.PreflightError):
            backup.discard_snapshot(args)
        assert snapshot.exists()
    else:
        assert backup.discard_snapshot(args) == {
            "result": "sanitized_source_snapshot_removed",
            "scope": policy.SCOPE,
        }
        assert not snapshot.exists()


@pytest.mark.parametrize("value", [None, [], {"sqlite_freelist_count": False}])
def test_scope_record_types_are_strict(value):
    if isinstance(value, dict):
        value = backup._scope_record() | value
    with pytest.raises(backup.scan.PreflightError):
        policy.validate_scope_record(value)


@pytest.mark.parametrize("fault", ["existing", "inside", "symlink"])
def test_cli_verify_does_not_overwrite_an_existing_or_unsafe_receipt(snapshot, tmp_path, fault):
    receipt = tmp_path / "receipt.json"
    if fault == "inside":
        receipt = snapshot / "receipt.json"
    elif fault == "symlink":
        target = tmp_path / "retain.txt"
        target.write_text("retain")
        receipt.symlink_to(target)
    else:
        receipt.write_text("retain")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(backup.__file__)),
            "verify",
            str(snapshot),
            "--receipt",
            str(receipt),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1 and not result.stderr
    assert json.loads(result.stdout)["result"] == "blocked"
    if fault != "inside":
        assert receipt.read_text() == "retain"
    else:
        assert not receipt.exists()


def run_isolated_helper_bundle(source, tmp_path):
    """Snapshot and verify with only the pinned helpers, in a directory that holds no repository."""
    # Exact helpers only: no runtime source, .env, sessions, datasets or model weights.
    helpers = tmp_path / "isolated-helper/scripts"
    helpers.mkdir(parents=True)
    (helpers / "__init__.py").write_text("")
    for name in (
        "sanitized_backup",
        "sanitized_contract",
        "data_only_backup",
        "data_only_preflight",
        "offhost_backup",
        "protected_recovery",
        "recovery",
    ):
        shutil.copyfile(Path(backup.__file__).parent / (name + ".py"), helpers / (name + ".py"))
    script = helpers / "sanitized_backup.py"
    application = str(recovery.ROOT)
    prepare = subprocess.run(
        [
            sys.executable,
            str(script),
            "snapshot",
            "--project-root",
            str(source.project_root),
            "--output-dir",
            str(source.output_dir),
            "--application-project-root",
            application,
            "--min-free-gib",
            "0",
            "--acknowledge-url-loss",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    receipt = tmp_path / "bundle-receipt.json"
    restore = subprocess.run(
        [
            sys.executable,
            str(script),
            "verify",
            str(source.output_dir),
            "--receipt",
            str(receipt),
            "--application-project-root",
            application,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    return helpers, prepare, restore, receipt


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the verify receipt is flushed with F_FULLFSYNC, which only macOS has",
)
def test_isolated_helper_bundle_runs_snapshot_and_restore_against_separate_application(
    source, tmp_path
):
    helpers, prepare, restore, receipt = run_isolated_helper_bundle(source, tmp_path)
    assert prepare.returncode == 0, prepare.stdout
    assert json.loads(prepare.stdout)["result"] == "sanitized_source_snapshot_created"
    assert restore.returncode == 0, restore.stdout
    result = backup.read_json(receipt)
    assert result["result"] == "data_restore_verified"
    assert result["listing_albums"] == backup.ALBUM_RESULT | {"network": "disabled"}
    assert result["offhost_verified"] is False
    assert result["protected_recovery_verified"] is False
    assert not (helpers.parent / "src").exists()


def test_isolated_helper_bundle_creates_the_snapshot_and_refuses_the_receipt_off_macos(
    source, tmp_path
):
    helpers, prepare, restore, receipt = run_isolated_helper_bundle(source, tmp_path)
    assert prepare.returncode == 0, prepare.stdout
    assert json.loads(prepare.stdout)["result"] == "sanitized_source_snapshot_created"
    if sys.platform != "darwin":
        # The receipt is flushed with F_FULLFSYNC, which only macOS has. Off macOS the verify
        # step must stop before the receipt exists.
        assert restore.returncode == 1
        assert json.loads(restore.stdout) == {
            "result": "blocked",
            "reason": "full_flush_unavailable",
        }
        assert not receipt.exists()
    else:
        assert restore.returncode == 0, restore.stdout
        assert receipt.exists()
    assert not (helpers.parent / "src").exists()


def test_lease_release_failure_cannot_publish_a_generation_or_receipt(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)

    @contextmanager
    def lost_at_exit(_args):
        yield
        raise backup.scan.PreflightError("source_lease_release_failed")

    monkeypatch.setattr(backup.transport, "remote_lease", lost_at_exit)
    with pytest.raises(backup.scan.PreflightError, match="source_lease_release_failed"):
        backup.receive(args)
    assert not list((args.private_root / "generations").iterdir())
    assert not list((args.private_root / "receipts").iterdir())
    # The source snapshot is already discarded, so the verified copy is kept for an operator.
    assert (args.private_root / "pending" / args.source_snapshot.name / "manifest.json").exists()


def test_receipt_write_failure_retains_only_an_unaccepted_generation(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    write = backup._durable_write

    def fail_receipt(path, value):
        if path.parent.name == "receipts":
            raise PermissionError("synthetic write failure")
        return write(path, value)

    monkeypatch.setattr(backup, "_durable_write", fail_receipt)
    with pytest.raises(PermissionError):
        backup.receive(args)
    assert not list((args.private_root / "receipts").iterdir())
    assert not list((args.private_root / "pending").iterdir())
    generation = args.private_root / "generations" / args.source_snapshot.name
    assert generation.is_dir()
    with pytest.raises(backup.scan.PreflightError, match="generation_exists"):
        backup.receive(args)


def test_pre_lease_checks_leave_another_receivers_temporaries_alone(tmp_path, monkeypatch):
    """A temporary another receiver is writing is not removed before the lease, and its write works.

    The namespace is bound and checked before the lease, so the refusal below happens with the
    temporaries in place. The receiver that wrote them can still finish its rename afterwards.
    """
    args = receiver_args(tmp_path)
    root = args.private_root
    generation = args.source_snapshot.name
    root.mkdir(parents=True, mode=0o700)
    (root / "resume").mkdir(mode=0o700)
    (root / "receipts").mkdir(mode=0o700)
    (root / "scope.json").write_text(
        json.dumps({"format": policy.FORMAT, "scope": policy.SCOPE}, sort_keys=True)
    )
    writing = [
        root / f"scope.{uuid.uuid4().hex}.tmp",
        root / "resume" / f"{generation}.{uuid.uuid4().hex}.tmp",
        root / "receipts" / f"{generation}.{uuid.uuid4().hex}.tmp",
    ]
    for path in writing:
        path.write_text("{}")

    @contextmanager
    def busy(_args):
        raise backup.scan.PreflightError("source_lease_busy")
        yield  # pragma: no cover

    monkeypatch.setattr(backup.transport, "remote_lease", busy)
    with pytest.raises(backup.scan.PreflightError, match=r"^source_lease_busy$"):
        backup.receive(args)
    assert all(path.exists() for path in writing)
    for path in writing:
        os.replace(path, path.with_suffix(".renamed"))  # the other writer's rename still works


def test_stale_resume_temporary_is_removed_under_the_lease_and_does_not_block_a_fresh_run(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    resume = args.private_root / "resume"
    args.private_root.mkdir(parents=True, mode=0o700)
    resume.mkdir(mode=0o700)
    stale = resume / f"{args.source_snapshot.name}.{uuid.uuid4().hex}.tmp"
    stale.write_text("{}")  # left behind by a receiver that was killed while it wrote a record
    result = backup.receive(args)
    assert result["offhost_verified"] is True
    assert not stale.exists()


def smoke_child_result():
    return {
        "application": {
            "catalog": 200,
            "apartment": 200,
            "photo": 200,
            "photo_decoded": True,
            "network": "disabled",
        },
        "listing_albums": backup.ALBUM_RESULT | {"network": "disabled"},
    }


def killed_child(command, **kwargs):
    """A child that wrote a database copy into its scratch and was killed at the timeout."""
    scratch = Path(command[command.index("_smoke") + 2])
    (scratch / "admin-copy.db").write_bytes(b"synthetic copy of the database")
    raise subprocess.TimeoutExpired(command, kwargs["timeout"])


def test_restore_scratch_is_made_by_the_parent_and_removed_after_the_child(snapshot, monkeypatch):
    seen = {}

    def child(command, **_kwargs):
        scratch = Path(command[command.index("_smoke") + 2])
        seen.update(path=scratch, parent=scratch.parent, mode=scratch.stat().st_mode & 0o777)
        (scratch / "synthetic.db").write_text("synthetic only")
        return SimpleNamespace(returncode=0, stdout=json.dumps(smoke_child_result()))

    monkeypatch.setattr(backup.subprocess, "run", child)
    result = backup.verify_snapshot(snapshot, application=True)
    assert result["result"] == "data_restore_verified"
    # Outside the snapshot namespace, so a killed CLI process cannot leave a scratch inside it.
    assert seen["parent"].resolve() == Path(tempfile.gettempdir()).resolve()
    assert seen["mode"] == 0o700
    assert not seen["path"].exists()


def test_child_refuses_a_scratch_that_is_not_a_dedicated_restore_directory(
    snapshot, tmp_path, monkeypatch
):
    plain = tmp_path / "plain-directory"
    plain.mkdir()
    with pytest.raises(backup.scan.PreflightError, match=r"^invalid_restore_scratch$"):
        backup._smoke(snapshot, plain)


def test_killed_smoke_child_leaves_no_scratch_next_to_or_inside_the_snapshot(snapshot, monkeypatch):
    monkeypatch.setattr(backup.subprocess, "run", killed_child)
    with pytest.raises(subprocess.TimeoutExpired):
        backup.verify_snapshot(snapshot, application=True)
    assert not list(snapshot.parent.glob(".sanitized-restore-*"))
    assert not list(snapshot.glob(".sanitized-restore-*"))


def test_receive_removes_the_copy_scratch_when_the_child_is_killed(snapshot, tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    seen = {}

    def killed_in_the_copy(command, **kwargs):
        seen["parent"] = Path(command[command.index("_smoke") + 2]).parent
        return killed_child(command, **kwargs)

    monkeypatch.setattr(backup.subprocess, "run", killed_in_the_copy)
    with pytest.raises(subprocess.TimeoutExpired):
        backup.receive(args)
    root, generation = args.private_root, args.source_snapshot.name
    assert seen["parent"] == root / "pending" / generation  # the scratch lived inside the copy
    assert not list(root.rglob(".sanitized-restore-*"))
    assert not (root / "pending" / generation).exists()
    record = backup.read_json(root / "resume" / (generation + ".json"))
    assert record["state"] == backup.TRANSFERRING


def test_snapshot_has_explicit_long_deadline_and_retains_guarded_transport(tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    observed = []
    expected = object()

    def guarded(command, **kwargs):
        observed.append((command, kwargs))
        return expected

    monkeypatch.setattr(backup.transport, "run", guarded)
    command = ["python", "/tmp/synthetic-helper.py", "snapshot"]
    assert backup._snapshot_remote(args, command) is expected
    assert observed == [
        (
            [
                *backup.transport.ssh(args),
                backup.shlex.join([*backup.transport.prefix(args), *command]),
            ],
            {"timeout": 21600},
        )
    ]


def test_progress_contains_only_aggregate_counts_and_never_enters_generation(source, monkeypatch):
    records = []
    original = backup.transport.atomic_json

    def capture(path, value):
        if path.name == "progress.json":
            assert set(value) == {"phase", "completed", "total", "bytes_done", "at_utc"}
            assert value["phase"] in {
                "freeze_database",
                "select_media",
                "inspect_source_media",
                "copy_frozen_media",
                "verify_frozen_snapshot",
            }
            assert all(
                type(value[k]) is int and value[k] >= 0
                for k in ("completed", "total", "bytes_done")
            )
            records.append(dict(value))
        return original(path, value)

    monkeypatch.setattr(backup.transport, "atomic_json", capture)
    backup.create_snapshot(source)
    for phase in ("inspect_source_media", "copy_frozen_media", "verify_frozen_snapshot"):
        phase_records = [r for r in records if r["phase"] == phase]
        assert phase_records[0]["completed"] == 0
        assert phase_records[-1]["completed"] == phase_records[-1]["total"]
        assert phase_records[-1]["bytes_done"] > 0
    assert not (source.output_dir / "progress.json").exists()
    assert not list(source.output_dir.parent.glob(".sanitized-build-*"))


def test_operator_interrupt_cleans_temporary_snapshot_and_publishes_nothing(source, monkeypatch):
    original = backup.scan.inspect_image
    calls = 0

    def interrupt(path):
        nonlocal calls
        calls += 1
        if calls == 150:
            raise KeyboardInterrupt()
        return original(path)

    monkeypatch.setattr(backup.scan, "inspect_image", interrupt)
    with pytest.raises(KeyboardInterrupt):
        backup.create_snapshot(source)
    assert not source.output_dir.exists()
    assert not list(source.output_dir.parent.glob(".sanitized-build-*"))


def test_source_discards_original_database_before_scanning_any_media(source, monkeypatch):
    original = backup._progress
    phases = []

    def check(work, phase, *args, **kwargs):
        if phase in {
            "select_media",
            "inspect_source_media",
            "copy_frozen_media",
            "verify_frozen_snapshot",
        }:
            assert not (work / "baseline.db").exists()
            phases.append(phase)
        return original(work, phase, *args, **kwargs)

    monkeypatch.setattr(backup, "_progress", check)
    before = recovery.digest(source.db)
    backup.create_snapshot(source)
    assert set(phases) == {
        "select_media",
        "inspect_source_media",
        "copy_frozen_media",
        "verify_frozen_snapshot",
    }
    assert recovery.digest(source.db) == before


def test_source_reuses_verified_image_bytes_but_receiver_scans_independently(source, monkeypatch):
    original = backup.scan.inspect_image
    observed = []

    def inspect(path):
        if path.name == "photo.jpg":
            observed.append(path)
        return original(path)

    monkeypatch.setattr(backup.scan, "inspect_image", inspect)
    backup.create_snapshot(source)
    assert observed == [source.project_root / "data/inbound/photo.jpg"]
    backup.verify_snapshot(source.output_dir)
    assert observed == [
        source.project_root / "data/inbound/photo.jpg",
        source.output_dir / "data/inbound/photo.jpg",
    ]


def test_corrupted_frozen_bytes_fail_before_reusing_scan_results(source, monkeypatch):
    original = backup._progress
    mutated = False

    def corrupt(work, phase, *args, **kwargs):
        nonlocal mutated
        if phase == "verify_frozen_snapshot" and not mutated:
            target = work / "snapshot/data/inbound/photo.jpg"
            raw = target.read_bytes()
            target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
            mutated = True
        return original(work, phase, *args, **kwargs)

    monkeypatch.setattr(backup, "_progress", corrupt)
    with pytest.raises(backup.scan.PreflightError, match="snapshot_checksum_differs"):
        backup.create_snapshot(source)
    assert mutated and not source.output_dir.exists()
    assert not list(source.output_dir.parent.glob(".sanitized-build-*"))


@pytest.mark.parametrize("fault", ["inventory", "hash", "size", "dimensions"])
def test_scan_reuse_rejects_mismatched_or_malformed_proof(snapshot, fault):
    manifest = manifest_at(snapshot)
    scanned = {
        name: backup.scan.inspect_image(snapshot / name)
        for name in manifest["files"]
        if name not in {"data/otz.db", "data/photo_manifest.json"}
    }
    key = "data/inbound/photo.jpg"
    if fault == "inventory":
        scanned.pop(key)
    elif fault == "hash":
        scanned[key]["sha256"] = "0" * 64
    elif fault == "size":
        scanned[key]["size"] += 1
    else:
        scanned[key]["width"] = -1
    with pytest.raises(backup.scan.PreflightError):
        backup.verify_snapshot(snapshot, _scanned_media=scanned)


def test_actual_restore_never_accepts_reused_source_scan_proof(snapshot):
    with pytest.raises(backup.scan.PreflightError, match="scan_reuse_for_restore_forbidden"):
        backup.verify_snapshot(snapshot, application=True, _scanned_media={})


def test_root_operator_is_refused_before_any_transport(tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    args.remote_user = "root"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("transport before the operator check")

    monkeypatch.setattr(backup.transport, "remote_lease", forbidden)
    with pytest.raises(backup.scan.PreflightError, match=r"^unsupported_source_operator$"):
        backup.receive(args)


def test_lost_snapshot_answer_leaves_a_requested_record_that_blocks_fresh_runs(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)

    def answer_lost(*_args, **_kwargs):
        raise backup.scan.PreflightError("snapshot_answer_lost")

    monkeypatch.setattr(backup, "_snapshot_remote", answer_lost)
    with pytest.raises(backup.scan.PreflightError, match=r"^snapshot_answer_lost$"):
        backup.receive(args)
    record = backup.read_json(args.private_root / "resume" / (args.source_snapshot.name + ".json"))
    assert record["state"] == "requested"
    assert record["manifest_sha256"] is None
    with pytest.raises(backup.scan.PreflightError, match=r"^unresolved_resume_record$"):
        backup.receive(args)
    args.resume_manifest_sha256 = recovery.digest(snapshot / "manifest.json")
    with pytest.raises(backup.scan.PreflightError, match=r"^resume_requires_operator$"):
        backup.receive(args)


def test_resume_replaces_a_partial_pending_copy_left_by_an_interrupted_transfer(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch, "manifest")
    with pytest.raises(backup.scan.PreflightError):
        backup.receive(args)
    generation = args.source_snapshot.name
    partial = args.private_root / "pending" / generation
    partial.mkdir(mode=0o700)
    (partial / "partial-transfer.bin").write_bytes(b"half a file")
    args.resume_manifest_sha256 = recovery.digest(snapshot / "manifest.json")
    fake_transport(snapshot, args, monkeypatch)
    result = backup.receive(args)
    assert result["offhost_verified"] is True
    assert not (args.private_root / "pending" / generation).exists()
    assert (args.private_root / "generations" / generation).exists()


def test_copy_is_flushed_before_the_source_is_discarded(snapshot, tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    order = []
    flush = backup._fsync_tree
    discard = backup._discard_remote
    monkeypatch.setattr(
        backup, "_fsync_tree", lambda folder: (order.append("fsync"), flush(folder))[1]
    )
    monkeypatch.setattr(
        backup, "_discard_remote", lambda a, checksum: (order.append("discard"), discard(a, checksum))
    )
    result = backup.receive(args)
    assert order == ["fsync", "discard"]
    assert result["offhost_verified"] is True


class PowerLoss(BaseException):
    """Simulated power loss: nothing after the armed point reaches the disk or the server."""


@pytest.fixture
def power(snapshot, tmp_path, monkeypatch):
    from scripts import bounded_transfer

    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    real_batches = bounded_transfer.batches
    monkeypatch.setattr(
        bounded_transfer, "batches", lambda names: real_batches(names, max_files=40)
    )
    state = SimpleNamespace(point=None, dead=False, present=False, batches=0)
    source_remote = backup.transport.remote
    source_snapshot = backup._snapshot_remote
    source_run = backup.transport.run
    real_write = backup._durable_write
    real_verify = backup.verify_snapshot
    real_discard = backup._discard_remote
    real_publish = backup._publish_generation
    real_rmtree = shutil.rmtree

    def guard(function):
        def guarded(*call_args, **call_kwargs):
            if state.dead:
                raise PowerLoss()
            return function(*call_args, **call_kwargs)

        return guarded

    def reached(point):
        if state.point == point:
            state.dead = True
            raise PowerLoss()

    def remote(remote_args, command):
        if "inspect-snapshot" not in command:
            return source_remote(remote_args, command)
        if not state.present:
            return SimpleNamespace(
                stdout=json.dumps({"result": "snapshot_absent", "scope": policy.SCOPE})
            )
        digest = recovery.digest(snapshot / "manifest.json")
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "result": "snapshot_present",
                    "scope": policy.SCOPE,
                    "manifest_sha256": digest,
                }
            )
        )

    def snapshot_rpc(remote_args, command):
        result = source_snapshot(remote_args, command)
        state.present = True
        reached("snapshot_rpc")
        return result

    def discard(remote_args, checksum):
        real_discard(remote_args, checksum)
        state.present = False
        reached("discard")

    def write(path, value):
        real_write(path, value)
        if path.parent.name == "receipts":
            reached("receipt")
        elif "state" in value:
            reached(value["state"])

    def verify(folder, **kwargs):
        result = real_verify(folder, **kwargs)
        reached("verify")
        return result

    def publish(pending, destination):
        real_publish(pending, destination)
        reached("published")

    def transfer(command):
        source_run(command)
        if "--files-from" in command:
            state.batches += 1
            reached(f"batch_{state.batches}")

    monkeypatch.setattr(backup.transport, "remote", guard(remote))
    monkeypatch.setattr(backup, "_snapshot_remote", guard(snapshot_rpc))
    monkeypatch.setattr(backup.transport, "run", guard(transfer))
    monkeypatch.setattr(backup, "_durable_write", guard(write))
    monkeypatch.setattr(backup, "verify_snapshot", guard(verify))
    monkeypatch.setattr(backup, "_discard_remote", guard(discard))
    monkeypatch.setattr(backup, "_publish_generation", guard(publish))
    monkeypatch.setattr(shutil, "rmtree", guard(real_rmtree))
    return SimpleNamespace(args=args, snapshot=snapshot, state=state)


# Durable points in receive(): the record state left on disk, where the copy is, and what the
# restart must do. "resume" means the record allows it; "operator" means it must refuse.
CRASH_CASES = [
    ("retained", "retained", None, "resume"),
    ("transferring", "transferring", None, "resume"),
    ("batch_2", "transferring", "pending", "resume"),
    ("batch_4", "transferring", "pending", "resume"),
    ("verifying", "verifying", "pending", "operator"),
    ("verify", "verifying", "pending", "operator"),
    ("verified", "verified", "pending", "operator"),
    ("kept_for_operator", "kept_for_operator", "pending", "operator"),
    ("discard", "kept_for_operator", "pending", "operator"),
    ("published", "kept_for_operator", "generations", "operator"),
    ("receipt", "kept_for_operator", "generations", "operator"),
]


def test_copy_directory_entry_is_flushed_before_the_source_is_discarded(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    order = []
    real_fsync_path = backup._fsync_path
    monkeypatch.setattr(
        backup,
        "_fsync_path",
        lambda path: (order.append(("fsync", Path(path))), real_fsync_path(path))[1],
    )
    real_discard = backup._discard_remote
    monkeypatch.setattr(
        backup,
        "_discard_remote",
        lambda remote_args, checksum: (
            order.append(("discard", None)),
            real_discard(remote_args, checksum),
        )[1],
    )
    backup.receive(args)
    pending = args.private_root / "pending"
    generation = args.source_snapshot.name
    discard = order.index(("discard", None))
    assert order.index(("fsync", pending)) < discard  # the directory that holds the copy
    assert order.index(("fsync", pending / generation)) < order.index(("fsync", pending))


@pytest.mark.parametrize("point,state,copy_at,restart", CRASH_CASES)
def test_power_loss_at_each_durable_point_keeps_the_copy_and_reaches_the_documented_state(
    power, point, state, copy_at, restart
):
    args, generation = power.args, power.args.source_snapshot.name
    record = args.private_root / "resume" / (generation + ".json")
    digest = recovery.digest(power.snapshot / "manifest.json")
    power.state.point = point
    with pytest.raises(PowerLoss):
        backup.receive(args)
    power.state.dead, power.state.point = False, None  # restart: new process, same disk and server
    assert backup.read_json(record)["state"] == state
    if copy_at:
        assert (args.private_root / copy_at / generation / "manifest.json").exists()
    if restart == "resume":
        args.resume_manifest_sha256 = digest
        result = backup.receive(args)
        assert result["offhost_verified"] is True
        receipt = args.private_root / "receipts" / (generation + ".json")
        assert backup.read_json(receipt) == result
        assert not record.exists()
        assert (args.private_root / "generations" / generation / "manifest.json").exists()
        return
    with pytest.raises(
        backup.scan.PreflightError, match=r"^(unresolved_resume_record|generation_exists)$"
    ):
        backup.receive(args)
    args.resume_manifest_sha256 = digest
    with pytest.raises(backup.scan.PreflightError, match=r"^resume_requires_operator$"):
        backup.receive(args)
    assert backup.read_json(record)["state"] == state
    assert (args.private_root / copy_at / generation / "manifest.json").exists()


def test_verified_copy_survives_a_restart_after_the_source_is_gone(power):
    args, generation = power.args, power.args.source_snapshot.name
    digest = recovery.digest(power.snapshot / "manifest.json")
    power.state.point = "verified"
    with pytest.raises(PowerLoss):
        backup.receive(args)
    power.state.dead, power.state.point = False, None
    shutil.rmtree(power.snapshot)  # the source snapshot no longer exists on either side
    args.resume_manifest_sha256 = digest
    with pytest.raises(backup.scan.PreflightError, match=r"^resume_requires_operator$"):
        backup.receive(args)
    assert (args.private_root / "pending" / generation / "manifest.json").exists()


def test_lost_snapshot_answer_is_resolved_by_keep_and_then_resumed(power):
    args, generation = power.args, power.args.source_snapshot.name
    record = args.private_root / "resume" / (generation + ".json")
    power.state.point = "snapshot_rpc"
    with pytest.raises(PowerLoss):
        backup.receive(args)
    power.state.dead, power.state.point = False, None
    assert backup.read_json(record)["state"] == backup.REQUESTED
    with pytest.raises(backup.scan.PreflightError, match=r"^unresolved_resume_record$"):
        backup.receive(args)
    request_id = backup.read_json(record)["request_id"]
    args.request_id, args.decision = request_id, "keep"
    kept = backup.resolve_requested(args)
    kept_record = backup.read_json(record)
    assert kept_record["state"] == backup.RETAINED and kept_record["request_id"] == request_id
    assert kept == {
        "result": "requested_record_kept",
        "generation": generation,
        "manifest_sha256": recovery.digest(power.snapshot / "manifest.json"),
    }
    args.resume_manifest_sha256 = kept["manifest_sha256"]
    assert backup.receive(args)["offhost_verified"] is True
    assert not record.exists()


def test_crash_after_the_request_marker_is_abandoned_and_a_fresh_run_then_succeeds(power):
    args, generation = power.args, power.args.source_snapshot.name
    record = args.private_root / "resume" / (generation + ".json")
    power.state.point = "requested"
    with pytest.raises(PowerLoss):
        backup.receive(args)
    power.state.dead, power.state.point = False, None
    args.request_id = backup.read_json(record)["request_id"]
    args.decision = "keep"
    with pytest.raises(backup.scan.PreflightError, match=r"^remote_snapshot_absent$"):
        backup.resolve_requested(args)
    assert record.exists()
    args.decision = "abandon"
    assert backup.resolve_requested(args) == {
        "result": "requested_record_abandoned",
        "generation": generation,
    }
    assert not record.exists()
    assert backup.receive(args)["offhost_verified"] is True


def write_namespace(args):
    root = args.private_root
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    for name in ("pending", "generations", "receipts", "resume"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    (root / "scope.json").write_text(json.dumps({"format": policy.FORMAT, "scope": policy.SCOPE}))


REQUEST_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def requested_record(args):
    record = args.private_root / "resume" / (args.source_snapshot.name + ".json")
    record.write_text(
        json.dumps(backup._resume_record(args, None, REQUEST_ID, state=backup.REQUESTED))
    )
    args.request_id = REQUEST_ID  # the operator echoes the id the record carries
    return record


def scripted_inspect(monkeypatch, answer):
    def remote(_args, command):
        assert command[1:4] == ["-I", "-c", pin.HELPER_BOOT]
        assert "inspect-snapshot" in command
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(stdout=answer)

    monkeypatch.setattr(backup.transport, "remote", remote)
    monkeypatch.setattr(backup.transport, "remote_lease", fake_lease)


def absent_answer():
    return json.dumps({"result": "snapshot_absent", "scope": policy.SCOPE})


def present_answer(digest):
    return json.dumps(
        {"result": "snapshot_present", "scope": policy.SCOPE, "manifest_sha256": digest}
    )


@pytest.mark.parametrize(
    "decision,answer,code",
    [
        ("keep", absent_answer(), "remote_snapshot_absent"),
        ("abandon", present_answer("c" * 64), "remote_snapshot_present"),
    ],
)
def test_requested_resolution_refuses_when_the_remote_contradicts_the_decision(
    tmp_path, monkeypatch, decision, answer, code
):
    args = receiver_args(tmp_path)
    write_namespace(args)
    record = requested_record(args)
    scripted_inspect(monkeypatch, answer)
    args.decision = decision
    before = record.read_bytes()
    with pytest.raises(backup.scan.PreflightError, match=rf"^{code}$"):
        backup.resolve_requested(args)
    assert record.read_bytes() == before


@pytest.mark.parametrize("decision", ["keep", "abandon"])
@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        json.dumps({"result": "snapshot_absent", "scope": "db_media"}),
        json.dumps({"result": "snapshot_present", "scope": policy.SCOPE}),
        json.dumps(
            {"result": "snapshot_present", "scope": policy.SCOPE, "manifest_sha256": "XYZ"}
        ),
        json.dumps({"result": "snapshot_absent", "scope": policy.SCOPE, "extra": 1}),
    ],
)
def test_ambiguous_remote_answer_refuses_both_decisions_and_changes_nothing(
    tmp_path, monkeypatch, decision, answer
):
    args = receiver_args(tmp_path)
    write_namespace(args)
    record = requested_record(args)
    scripted_inspect(monkeypatch, answer)
    args.decision = decision
    before = record.read_bytes()
    with pytest.raises(backup.scan.PreflightError, match=r"^remote_state_ambiguous$"):
        backup.resolve_requested(args)
    assert record.read_bytes() == before


@pytest.mark.parametrize("decision", ["keep", "abandon"])
def test_remote_failure_during_resolution_refuses_and_changes_nothing(
    tmp_path, monkeypatch, decision
):
    args = receiver_args(tmp_path)
    write_namespace(args)
    record = requested_record(args)
    scripted_inspect(monkeypatch, subprocess.CalledProcessError(255, "ssh"))
    args.decision = decision
    before = record.read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        backup.resolve_requested(args)
    assert record.read_bytes() == before


def test_requested_record_is_removed_only_on_a_proven_absent_abandon(tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    write_namespace(args)
    record = requested_record(args)
    scripted_inspect(monkeypatch, absent_answer())
    args.decision = "abandon"
    assert backup.resolve_requested(args) == {
        "result": "requested_record_abandoned",
        "generation": args.source_snapshot.name,
    }
    assert not record.exists()


def forbidden_remote(*_args, **_kwargs):
    raise AssertionError("remote contact although the local request check refused")


def test_requested_keep_records_a_retained_state_under_the_same_request(tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    write_namespace(args)
    record = requested_record(args)
    scripted_inspect(monkeypatch, present_answer("c" * 64))
    args.decision = "keep"
    assert backup.resolve_requested(args) == {
        "result": "requested_record_kept",
        "generation": args.source_snapshot.name,
        "manifest_sha256": "c" * 64,
    }
    assert backup.read_json(record) == backup._resume_record(
        args, "c" * 64, REQUEST_ID, state=backup.RETAINED
    )


@pytest.mark.parametrize("decision", ["keep", "abandon"])
@pytest.mark.parametrize("echo", ["0" * 32, "not-a-request-id"])
def test_resolution_refuses_an_echo_that_is_not_the_records_request(
    tmp_path, monkeypatch, decision, echo
):
    args = receiver_args(tmp_path)
    write_namespace(args)
    record = requested_record(args)
    monkeypatch.setattr(backup.transport, "remote_lease", forbidden_remote)
    args.request_id, args.decision = echo, decision
    before = record.read_bytes()
    with pytest.raises(backup.scan.PreflightError, match=r"^(request_id_mismatch|invalid_request_id)$"):
        backup.resolve_requested(args)
    assert record.read_bytes() == before


@pytest.mark.parametrize("decision", ["keep", "abandon"])
def test_resolution_refuses_a_record_written_before_request_ids_existed(
    tmp_path, monkeypatch, decision
):
    args = receiver_args(tmp_path)
    write_namespace(args)
    record = args.private_root / "resume" / (args.source_snapshot.name + ".json")
    legacy = backup._resume_record(args, None, REQUEST_ID, state=backup.REQUESTED)
    del legacy["request_id"]
    record.write_text(json.dumps(legacy))
    monkeypatch.setattr(backup.transport, "remote_lease", forbidden_remote)
    args.request_id, args.decision = REQUEST_ID, decision
    with pytest.raises(backup.scan.PreflightError, match=r"^request_id_mismatch$"):
        backup.resolve_requested(args)


@pytest.mark.parametrize("decision", ["keep", "abandon"])
def test_resolution_refuses_a_record_written_by_another_helper_set(tmp_path, monkeypatch, decision):
    args = receiver_args(tmp_path)
    write_namespace(args)
    record = args.private_root / "resume" / (args.source_snapshot.name + ".json")
    other = backup._resume_record(args, None, REQUEST_ID, state=backup.REQUESTED)
    other["helper_set_sha256"] = "e" * 64
    record.write_text(json.dumps(other))
    monkeypatch.setattr(backup.transport, "remote_lease", forbidden_remote)
    args.request_id, args.decision = REQUEST_ID, decision
    with pytest.raises(backup.scan.PreflightError, match=r"^helper_pin_mismatch$"):
        backup.resolve_requested(args)


@pytest.mark.parametrize("decision", ["keep", "abandon"])
def test_resolution_refuses_while_a_source_build_holds_the_lock(tmp_path, monkeypatch, decision):
    args = receiver_args(tmp_path)
    write_namespace(args)
    record = requested_record(args)
    scripted_inspect(monkeypatch, json.dumps({"result": "snapshot_build_running", "scope": policy.SCOPE}))
    args.decision = decision
    before = record.read_bytes()
    with pytest.raises(backup.scan.PreflightError, match=r"^remote_build_running$"):
        backup.resolve_requested(args)
    assert record.read_bytes() == before


def test_build_lock_is_held_for_the_build_and_the_probe_never_creates_it(tmp_path):
    assert backup._build_running(tmp_path) is False
    assert not (tmp_path / backup.BUILD_LOCK).exists()  # the probe creates nothing
    with backup._build_lock(tmp_path):
        assert backup._build_running(tmp_path) is True
    assert backup._build_running(tmp_path) is False  # released with the build


def test_a_build_is_refused_while_another_build_holds_the_namespace_lock(source):
    with (
        backup._build_lock(source.output_dir.absolute().parent),
        pytest.raises(backup.scan.PreflightError, match=r"^snapshot_build_running$"),
    ):
        backup.create_snapshot(source)
    assert not source.output_dir.exists()


def test_inspect_reports_a_running_build_before_reporting_absence(tmp_path, monkeypatch):
    folder = tmp_path / "20261006-090000-123abc45"
    pattern = re.escape(str(tmp_path)) + r"/[0-9]{8}-[0-9]{6}-[a-f0-9]{8}"
    monkeypatch.setattr(backup, "SOURCE_SNAPSHOT", re.compile(pattern))
    request = SimpleNamespace(directory=folder)
    with backup._build_lock(tmp_path):
        assert backup.inspect_snapshot(request) == {
            "result": "snapshot_build_running",
            "scope": policy.SCOPE,
        }
    assert backup.inspect_snapshot(request) == {"result": "snapshot_absent", "scope": policy.SCOPE}


def test_resolution_refuses_other_states_and_missing_records_before_remote_contact(
    tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    write_namespace(args)
    args.request_id, args.decision = REQUEST_ID, "keep"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("remote contact before the local record check")

    monkeypatch.setattr(backup.transport, "remote_lease", forbidden)
    with pytest.raises(backup.scan.PreflightError, match=r"^requested_record_missing$"):
        backup.resolve_requested(args)
    record = args.private_root / "resume" / (args.source_snapshot.name + ".json")
    record.write_text(
        json.dumps(
            backup._resume_record(args, "d" * 64, REQUEST_ID, state=backup.RETAINED)
        )
    )
    with pytest.raises(backup.scan.PreflightError, match=r"^requested_record_mismatch$"):
        backup.resolve_requested(args)
    args.decision = "delete"
    with pytest.raises(backup.scan.PreflightError, match=r"^invalid_decision$"):
        backup.resolve_requested(args)


def test_resume_refuses_a_requested_record_and_points_to_the_operator(tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    write_namespace(args)
    requested_record(args)
    args.resume_manifest_sha256 = "a" * 64

    def forbidden(*_args, **_kwargs):
        raise AssertionError("transport before the operator check")

    monkeypatch.setattr(backup.transport, "remote_lease", forbidden)
    with pytest.raises(backup.scan.PreflightError, match=r"^resume_requires_operator$"):
        backup.receive(args)


def test_record_written_while_waiting_for_the_lease_blocks_a_fresh_run(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    write_namespace(args)
    fake_transport(snapshot, args, monkeypatch)
    rival = args.private_root / "resume" / (args.source_snapshot.name + ".json")

    rival_copy = args.private_root / "pending" / args.source_snapshot.name

    @contextmanager
    def lease_after_a_rival_requests(_args):
        # Another receiver recorded its request and copy while this one was waiting for the lease.
        rival.write_text(json.dumps(backup._resume_record(args, None, REQUEST_ID, state=backup.REQUESTED)))
        rival_copy.mkdir()
        (rival_copy / "rival.bin").write_bytes(b"rival copy")
        yield

    def forbidden(*_args, **_kwargs):
        raise AssertionError("snapshot or transfer although a record is open")

    monkeypatch.setattr(backup.transport, "remote_lease", lease_after_a_rival_requests)
    monkeypatch.setattr(backup, "_snapshot_remote", forbidden)
    monkeypatch.setattr(backup.transport, "run", forbidden)
    with pytest.raises(backup.scan.PreflightError, match=r"^unresolved_resume_record$"):
        backup.receive(args)
    assert backup.read_json(rival)["state"] == backup.REQUESTED  # the rival's record is untouched
    assert (rival_copy / "rival.bin").read_bytes() == b"rival copy"  # nor its copy


def test_resume_rechecks_its_record_under_the_lease_before_deleting_the_copy(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    write_namespace(args)
    fake_transport(snapshot, args, monkeypatch)
    generation = args.source_snapshot.name
    digest = recovery.digest(snapshot / "manifest.json")
    record = args.private_root / "resume" / (generation + ".json")
    record.write_text(json.dumps(backup._resume_record(args, digest, REQUEST_ID, state=backup.RETAINED)))
    partial = args.private_root / "pending" / generation
    partial.mkdir()
    (partial / "half.bin").write_bytes(b"copied by the earlier receiver")

    @contextmanager
    def lease_after_a_rival_verifies(_args):
        # Another receiver verified this copy while this one waited for the lease.
        record.write_text(json.dumps(backup._resume_record(args, digest, REQUEST_ID, state=backup.VERIFIED)))
        yield

    monkeypatch.setattr(backup.transport, "remote_lease", lease_after_a_rival_verifies)
    args.resume_manifest_sha256 = digest
    with pytest.raises(backup.scan.PreflightError, match=r"^resume_requires_operator$"):
        backup.receive(args)
    assert (partial / "half.bin").read_bytes() == b"copied by the earlier receiver"


def test_leftover_temporaries_never_block_a_fresh_run(snapshot, tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    write_namespace(args)
    leftover = args.private_root / "resume" / (
        args.source_snapshot.name + "." + "0" * 32 + ".tmp"
    )
    leftover.write_text("{")
    fake_transport(snapshot, args, monkeypatch)
    assert backup.receive(args)["offhost_verified"] is True
    assert not leftover.exists()


def test_failed_verification_records_an_unverified_copy_and_removes_it(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    real_verify = backup.verify_snapshot

    def failing_verify(*_args, **_kwargs):
        raise backup.scan.PreflightError("stale_snapshot")

    monkeypatch.setattr(backup, "verify_snapshot", failing_verify)
    with pytest.raises(backup.scan.PreflightError, match=r"^stale_snapshot$"):
        backup.receive(args)
    generation = args.source_snapshot.name
    record = backup.read_json(args.private_root / "resume" / (generation + ".json"))
    assert record["state"] == backup.TRANSFERRING
    assert not (args.private_root / "pending" / generation).exists()
    # The source was never discarded, so the generation can be transferred and verified again.
    monkeypatch.setattr(backup, "verify_snapshot", real_verify)
    args.resume_manifest_sha256 = recovery.digest(snapshot / "manifest.json")
    assert backup.receive(args)["offhost_verified"] is True


def test_durable_write_flushes_the_temporary_file_renames_then_flushes_the_directory(
    tmp_path, monkeypatch
):
    events = []
    real_replace = backup.os.replace

    def replace(source, target):
        events.append(("rename", Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(backup, "_fsync_path", lambda path: events.append(("fsync", Path(path))))
    monkeypatch.setattr(backup.os, "replace", replace)
    target = tmp_path / "record.json"
    backup._durable_write(target, {"state": "verified"})
    assert [kind for kind, _ in events] == ["fsync", "rename", "fsync"]
    temporary = events[0][1]
    assert temporary.parent == tmp_path and temporary.suffix == ".tmp"
    assert events[1] == ("rename", target)
    assert events[2] == ("fsync", tmp_path)
    assert json.loads(target.read_text()) == {"state": "verified"}


def test_flush_uses_f_fullfsync_and_never_falls_back_to_fsync(monkeypatch):
    calls = []
    refuse = {"value": False}

    def fake_fcntl(descriptor, command):
        calls.append(("F_FULLFSYNC", descriptor, command))
        if refuse["value"]:
            raise OSError(errno.ENOTSUP, "unsupported")
        return 0

    monkeypatch.setattr(backup, "fcntl", SimpleNamespace(F_FULLFSYNC=51, fcntl=fake_fcntl))
    monkeypatch.setattr(backup.os, "fsync", lambda descriptor: calls.append(("fsync", descriptor)))
    assert backup._flush(7) == "F_FULLFSYNC"
    refuse["value"] = True
    with pytest.raises(backup.scan.PreflightError, match=r"^full_flush_refused$"):
        backup._flush(8)
    assert calls == [("F_FULLFSYNC", 7, 51), ("F_FULLFSYNC", 8, 51)]


def test_flush_refuses_a_platform_without_f_fullfsync(monkeypatch):
    monkeypatch.setattr(backup, "fcntl", SimpleNamespace(fcntl=lambda descriptor, command: 0))
    with pytest.raises(backup.scan.PreflightError, match=r"^full_flush_unavailable$"):
        backup._flush(7)


@pytest.mark.skipif(sys.platform != "darwin", reason="F_FULLFSYNC is a macOS call")
def test_full_flush_on_macos_succeeds_for_files_and_directories_of_this_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.fcntl, "fcntl", REAL_FCNTL)
    probe = tmp_path / "probe.json"
    probe.write_bytes(b"{}")
    assert backup._fsync_path(probe) == "F_FULLFSYNC"
    assert backup._fsync_path(tmp_path) == "F_FULLFSYNC"


def test_refused_full_flush_on_the_copy_keeps_the_copy_and_never_discards_the_source(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    refusing = {"on": False}

    def declined(descriptor, command):
        if refusing["on"]:
            raise OSError(errno.ENOTSUP, "volume declines F_FULLFSYNC")
        return os.fsync(descriptor) or 0

    monkeypatch.setattr(backup.fcntl, "fcntl", declined)
    real_tree = backup._fsync_tree

    def copy_on_a_declining_volume(folder):
        # Only the copy's own flush is refused; the records written after it flush normally, so
        # the copy-path check is the one thing standing between the discard and the source.
        refusing["on"] = True
        try:
            return real_tree(folder)
        finally:
            refusing["on"] = False

    monkeypatch.setattr(backup, "_fsync_tree", copy_on_a_declining_volume)
    discards = []
    monkeypatch.setattr(backup, "_discard_remote", lambda *call: discards.append(call))
    with pytest.raises(backup.scan.PreflightError, match=r"^full_flush_refused$"):
        backup.receive(args)
    assert discards == []
    generation = args.source_snapshot.name
    record = backup.read_json(args.private_root / "resume" / (generation + ".json"))
    assert record["state"] == backup.VERIFIED  # not kept_for_operator: the discard never began
    assert (args.private_root / "pending" / generation / "manifest.json").exists()
    assert not (args.private_root / "receipts" / (generation + ".json")).exists()


def test_receipt_records_the_durability_mode_the_copy_was_flushed_with(
    snapshot, tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    result = backup.receive(args)
    receipt = backup.read_json(args.private_root / "receipts" / (args.source_snapshot.name + ".json"))
    assert result["durability_mode"] == receipt["durability_mode"] == "F_FULLFSYNC"


def test_fsync_tree_flushes_nested_directories_deepest_first_and_the_root_last(
    tmp_path, monkeypatch
):
    folder = tmp_path / "copy"
    for relative in ("data/listings/ID1", "data/inbound", "config"):
        (folder / relative).mkdir(parents=True)
    (folder / "manifest.json").write_text("{}")
    (folder / "data/listings/ID1/photo.jpg").write_bytes(b"x")
    (folder / "config/app.json").write_text("{}")
    order = []

    def record(path):
        order.append(Path(path).relative_to(folder).as_posix())

    monkeypatch.setattr(backup, "_fsync_path", record)
    backup._fsync_tree(folder)
    assert order[-1] == "."
    assert set(order) >= {
        "data/listings/ID1",
        "data/listings",
        "data",
        "data/inbound",
        "config",
        "manifest.json",
        "data/listings/ID1/photo.jpg",
    }
    assert order.index("data/listings/ID1/photo.jpg") < order.index("data/listings/ID1")
    assert order.index("data/listings/ID1") < order.index("data/listings") < order.index("data")


REPOSITORY = Path(__file__).resolve().parents[1]


def helper_tools(tmp_path):
    """The pinned helper set laid out as /tmp/otz-sanitized-tools-XXXX/scripts/ (copies only)."""
    tools = tmp_path / "otz-sanitized-tools-123abc45"
    for name in pin.HELPER_FILES:
        target = tools / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.copyfile(REPOSITORY / name, target)
        target.chmod(0o644)
    (tools / "scripts").chmod(0o755)
    return tools


def run_bootstrap(tools, *helper_args, entry="scripts.sanitized_backup", cwd=None):
    """Run the real bootstrap exactly as the receiver sends it, against a local directory."""
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            pin.HELPER_BOOT,
            str(tools),
            pin.HELPER_SET_SHA256,
            json.dumps(sorted(pin.HELPER_FILES)),
            entry,
            *helper_args,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        cwd=cwd,
    )


def test_source_bootstrap_runs_the_pinned_set_from_memory(tmp_path):
    result = run_bootstrap(helper_tools(tmp_path), "--help")
    assert result.returncode == 0
    assert result.stdout.startswith("usage: sanitized_backup.py")


def test_source_bootstrap_refuses_any_changed_byte_before_the_helper_runs(tmp_path):
    tools = helper_tools(tmp_path)
    with (tools / "scripts/sanitized_contract.py").open("ab") as handle:
        handle.write(b"\n# drift\n")
    result = run_bootstrap(tools, "--help")
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"result": "blocked", "reason": "remote_helper_digest_mismatch"}
    assert "usage:" not in result.stdout


@pytest.mark.parametrize("mode", [0o777, 0o775])
def test_source_bootstrap_refuses_a_tools_directory_under_a_writable_ancestor(tmp_path, mode):
    # The tools directory itself is safe; anyone who can rename its parent could replace it.
    shared = tmp_path / "shared"
    shared.mkdir()
    tools = helper_tools(shared)
    shared.chmod(mode)
    result = run_bootstrap(tools, "--help")
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"result": "blocked", "reason": "remote_helper_mode_unsafe"}


def test_source_bootstrap_accepts_a_sticky_ancestor_as_tmp_is(tmp_path):
    # Like /tmp: world-writable but sticky, so only the owner of an entry can rename it.
    shared = tmp_path / "shared"
    shared.mkdir()
    tools = helper_tools(shared)
    shared.chmod(0o1777)
    result = run_bootstrap(tools, "--help")
    assert result.returncode == 0
    assert result.stdout.startswith("usage: sanitized_backup.py")


@pytest.mark.parametrize("target", ["scripts/recovery.py", "scripts"])
def test_source_bootstrap_refuses_group_or_world_writable_files_and_directories(tmp_path, target):
    tools = helper_tools(tmp_path)
    path = tools / target
    path.chmod(0o664 if path.is_file() else 0o775)
    result = run_bootstrap(tools, "--help")
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"result": "blocked", "reason": "remote_helper_mode_unsafe"}


def test_source_bootstrap_refuses_files_not_owned_by_the_operator_or_root(tmp_path):
    if os.getuid() == 0:
        pytest.skip("as root every file is owned by root, which the bootstrap accepts")
    tools = helper_tools(tmp_path)
    # The wrapper only changes the euid the bootstrap compares with; the files keep their owner.
    wrapper = (
        "import os,sys; boot=sys.argv.pop(1); os.geteuid=lambda: 4242; "
        "exec(compile(boot,'<boot>','exec'))"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            wrapper,
            pin.HELPER_BOOT,
            str(tools),
            pin.HELPER_SET_SHA256,
            json.dumps(sorted(pin.HELPER_FILES)),
            "scripts.sanitized_backup",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"result": "blocked", "reason": "remote_helper_mode_unsafe"}


def test_source_bootstrap_never_imports_a_module_from_its_own_directory(tmp_path):
    tools = helper_tools(tmp_path)
    marker = tmp_path / "planted-module-ran"
    # shlex is imported by the helper; a copy beside the helper must never be the one that runs.
    (tools / "shlex.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    result = run_bootstrap(tools, "--help", cwd=tools)
    assert result.returncode == 0
    assert result.stdout.startswith("usage: sanitized_backup.py")
    assert not marker.exists()


def test_source_bootstrap_refuses_an_entry_outside_the_pinned_set(tmp_path):
    result = run_bootstrap(helper_tools(tmp_path), "--help", entry="scripts.bounded_transfer")
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"result": "blocked", "reason": "invalid_remote_helper_set"}


def test_import_hook_refuses_a_repository_module_outside_the_pinned_set(tmp_path):
    """Run-time refusal: the finder itself stops an import of an unpinned repository module.

    The entry is pinned, so the bootstrap runs. Its final import is replaced by an import of a
    repository module that is not in the set; only the finder can answer that import.
    """
    original = "module = importlib.import_module(entry)"
    assert original in pin.HELPER_BOOT
    boot = pin.HELPER_BOOT.replace(original, "module = importlib.import_module('scripts.bounded_transfer')")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            boot,
            str(helper_tools(tmp_path)),
            pin.HELPER_SET_SHA256,
            json.dumps(sorted(pin.HELPER_FILES)),
            "scripts.sanitized_backup",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 1
    assert "ImportError: module outside the pinned helper set" in result.stderr
    assert result.stdout == ""


def test_fresh_request_is_refused_while_a_source_build_runs_and_writes_no_record(
    tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    scripted_inspect(
        monkeypatch, json.dumps({"result": "snapshot_build_running", "scope": policy.SCOPE})
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("snapshot or transfer while a source build holds the lock")

    monkeypatch.setattr(backup, "_snapshot_remote", forbidden)
    monkeypatch.setattr(backup.transport, "run", forbidden)
    with pytest.raises(backup.scan.PreflightError, match=r"^remote_build_running$"):
        backup.receive(args)
    assert not list((args.private_root / "resume").iterdir())


def test_refused_bootstrap_stops_before_any_resume_record_snapshot_or_transfer(tmp_path, monkeypatch):
    """The lease and the bootstrap run are both ssh connections; the namespace binding (scope.json)
    is written locally before the lease. A refused bootstrap writes no resume record and starts
    no snapshot or transfer."""
    args = receiver_args(tmp_path)

    def refused(_args, command):
        assert command[1:4] == ["-I", "-c", pin.HELPER_BOOT]
        raise subprocess.CalledProcessError(1, "ssh")  # the bootstrap exits 1 on a refused set

    def forbidden(*_args, **_kwargs):
        raise AssertionError("snapshot or transfer after a refused bootstrap")

    monkeypatch.setattr(backup.transport, "remote", refused)
    monkeypatch.setattr(backup.transport, "remote_lease", fake_lease)
    monkeypatch.setattr(backup, "_snapshot_remote", forbidden)
    monkeypatch.setattr(backup.transport, "run", forbidden)
    with pytest.raises(subprocess.CalledProcessError):
        backup.receive(args)
    assert not list((args.private_root / "resume").iterdir())


def test_pinned_helper_set_matches_the_repository_files():
    files = {name: (REPOSITORY / name).read_bytes() for name in pin.HELPER_FILES}
    assert pin.helper_set_digest(files) == pin.HELPER_SET_SHA256


# Repository imports made inside a function body that only a receiver command reaches. The source
# commands are create_snapshot, inspect_snapshot and discard_snapshot; the test checks that none of
# these functions is reached from them. Anything else that is not pinned fails the test.
RECEIVER_ONLY_IMPORTS = {
    ("scripts/sanitized_backup.py", "_helper_command", "scripts/sanitized_helper_pin.py"),
    ("scripts/sanitized_backup.py", "_resume_record", "scripts/sanitized_helper_pin.py"),
    ("scripts/sanitized_backup.py", "_require_requested", "scripts/sanitized_helper_pin.py"),
    ("scripts/sanitized_backup.py", "receive", "scripts/bounded_transfer.py"),
}
SOURCE_COMMANDS = ("create_snapshot", "inspect_snapshot", "discard_snapshot")


def scripts_imports(path):
    """(repository file, enclosing function) for each import of a scripts module, at any depth.

    `import scripts.a.b`, `from scripts.a import b` and `from scripts import a` all count; for
    `from scripts import a` both `scripts` and `scripts.a` are checked. A relative import is
    reported as "<relative>", so it can never match the pinned set.
    """
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Import):
                dotted = [alias.name for alias in child.names]
            elif isinstance(child, ast.ImportFrom) and child.level:
                found.append(("<relative>", function))
                dotted = []
            elif isinstance(child, ast.ImportFrom):
                base = child.module or ""
                dotted = [base] + [f"{base}.{alias.name}" for alias in child.names]
            else:
                dotted = []
            for name in dotted:
                module = _scripts_module_file(name)
                if module is not None:
                    found.append((module, function))
            visit(child, inner)

    visit(ast.parse(path.read_text()), "<module>")
    return found


def _scripts_module_file(dotted):
    """scripts/<a>.py for the longest dotted prefix of a scripts name that is a file, else None."""
    parts = dotted.split(".")
    if parts[0] != "scripts":
        return None
    for end in range(len(parts), 1, -1):
        candidate = "/".join(parts[:end]) + ".py"
        if (REPOSITORY / candidate).is_file():
            return candidate
    return None


def functions_reached_from(path, roots):
    """Names of the module's functions reached by name from roots, following references in bodies."""
    functions = {
        node.name: node
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.FunctionDef)
    }
    reached, pending = set(), list(roots)
    while pending:
        name = pending.pop()
        if name in reached or name not in functions:
            continue
        reached.add(name)
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Name):
                pending.append(node.id)
            elif isinstance(node, ast.Attribute):
                pending.append(node.attr)
    return reached


def test_scripts_import_walk_sees_every_form_at_any_depth(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import scripts.bounded_transfer\n"
        "from scripts import bounded_transfer\n"
        "from scripts.bounded_transfer import batches\n"
        "from scripts import recovery as r\n"
        "import os\n"
        "def inner():\n"
        "    import scripts.sanitized_helper_pin\n"
        "    from scripts import bounded_transfer as b\n"
        "from . import sibling\n"
    )
    assert set(scripts_imports(sample)) == {
        ("scripts/bounded_transfer.py", "<module>"),
        ("scripts/recovery.py", "<module>"),
        ("scripts/sanitized_helper_pin.py", "inner"),
        ("scripts/bounded_transfer.py", "inner"),
        ("<relative>", "<module>"),
    }


def test_every_repository_import_of_the_source_helper_is_pinned_or_declared_receiver_only():
    found = {
        (name, function, module)
        for name in pin.HELPER_FILES
        for module, function in scripts_imports(REPOSITORY / name)
        if module not in pin.HELPER_FILES
    }
    assert found <= RECEIVER_ONLY_IMPORTS, sorted(found - RECEIVER_ONLY_IMPORTS)
    assert RECEIVER_ONLY_IMPORTS <= found, sorted(RECEIVER_ONLY_IMPORTS - found)
    source_side = functions_reached_from(REPOSITORY / "scripts/sanitized_backup.py", SOURCE_COMMANDS)
    declared = {function for name, function, _ in RECEIVER_ONLY_IMPORTS if name.endswith("sanitized_backup.py")}
    assert declared.isdisjoint(source_side), sorted(declared & source_side)


def test_receiver_refuses_an_interpreter_outside_the_documented_path_before_any_lease(
    tmp_path, monkeypatch
):
    args = receiver_args(tmp_path)
    args.remote_python = "python"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ssh although the interpreter is not the documented one")

    monkeypatch.setattr(backup.transport, "remote_lease", forbidden)
    monkeypatch.setattr(backup.transport, "remote", forbidden)
    with pytest.raises(backup.scan.PreflightError, match=r"^unsupported_remote_python$"):
        backup.receive(args)
