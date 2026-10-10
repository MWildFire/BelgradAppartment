"""Synthetic data only; every SSH/rsync boundary is replaced by a local fixture."""

import argparse
import copy
import json
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
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
        remote_python="python",
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


@pytest.mark.parametrize("remote_user", ["otz", "root"])
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
    assert not (args.private_root / "resume" / (generation + ".json")).exists()
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
    assert backup.read_json(record) == {
        "format": policy.FORMAT,
        "scope": policy.SCOPE,
        "generation": args.source_snapshot.name,
        "source_snapshot": str(args.source_snapshot),
        "manifest_sha256": recovery.digest(snapshot / "manifest.json"),
        "state": "retained_after_failure",
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
    with pytest.raises(backup.scan.PreflightError, match=r"^generation_exists$"):
        backup.receive(args)


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
    assert record["state"] == "kept_for_operator"
    for name in ("receipts", "generations"):
        assert not list((args.private_root / name).iterdir())


def test_record_write_failure_keeps_the_original_cause(snapshot, tmp_path, monkeypatch):
    args = receiver_args(tmp_path)
    fake_transport(snapshot, args, monkeypatch)
    write = backup.transport.atomic_json
    state = {"discard_attempted": False}

    def discard_fails(*_args, **_kwargs):
        state["discard_attempted"] = True
        raise backup.scan.PreflightError("source_snapshot_cleanup_unconfirmed")

    def write_fails_after_discard(path, value):
        if state["discard_attempted"] and "resume" in path.parts:
            raise OSError(28, "No space left on device")
        return write(path, value)

    monkeypatch.setattr(backup, "_discard_remote", discard_fails)
    monkeypatch.setattr(backup.transport, "atomic_json", write_fails_after_discard)
    with pytest.raises(backup.scan.PreflightError, match="source_snapshot_cleanup_unconfirmed") as info:
        backup.receive(args)
    assert any("resume record not updated: OSError" in note for note in info.value.__notes__)
    assert (args.private_root / "pending" / args.source_snapshot.name / "manifest.json").exists()


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
    "field,value",
    [
        ("manifest_sha256", "b" * 64),
        ("generation", "20261006-090001-123abc45"),
        ("source_snapshot", "/var/tmp/otz-data-only-sanitized/20261006-090001-123abc45"),
        ("state", "kept_for_operator"),
    ],
)
def test_resume_refuses_a_tampered_record_before_transport(
    snapshot, tmp_path, monkeypatch, field, value
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
    with pytest.raises(backup.scan.PreflightError, match=r"^resume_record_mismatch$"):
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


def test_child_network_guard_blocks_dns_and_connect_before_smoke(snapshot, monkeypatch):
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
    with pytest.raises(RuntimeError, match="guard_verified_stop"):
        backup._smoke(snapshot)


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


def test_isolated_helper_bundle_runs_snapshot_and_restore_against_separate_application(
    source, tmp_path
):
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
    assert prepare.returncode == 0, prepare.stdout
    assert json.loads(prepare.stdout)["result"] == "sanitized_source_snapshot_created"
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
    assert restore.returncode == 0, restore.stdout
    result = backup.read_json(receipt)
    assert result["result"] == "data_restore_verified"
    assert result["listing_albums"] == backup.ALBUM_RESULT | {"network": "disabled"}
    assert result["offhost_verified"] is False
    assert result["protected_recovery_verified"] is False
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
    write = backup.transport.atomic_json

    def fail_receipt(path, value):
        if path.parent.name == "receipts":
            raise PermissionError("synthetic write failure")
        return write(path, value)

    monkeypatch.setattr(backup.transport, "atomic_json", fail_receipt)
    with pytest.raises(PermissionError):
        backup.receive(args)
    assert not list((args.private_root / "receipts").iterdir())
    assert not list((args.private_root / "pending").iterdir())
    generation = args.private_root / "generations" / args.source_snapshot.name
    assert generation.is_dir()
    with pytest.raises(backup.scan.PreflightError, match="generation_exists"):
        backup.receive(args)


def test_restore_scratch_stays_in_backup_namespace_and_is_removed(snapshot, monkeypatch):
    previous = backup.tempfile.tempdir
    scratch = []

    def inspect(_folder):
        root = Path(backup.tempfile.gettempdir())
        assert root.parent == snapshot.parent and root.stat().st_mode & 0o777 == 0o700
        with backup.tempfile.TemporaryDirectory() as temporary:
            assert Path(temporary).parent == root
            scratch.append(Path(temporary))
            (Path(temporary) / "synthetic.db").write_text("synthetic only")
        scratch.append(root)
        return {"test": "scratch_isolated"}

    monkeypatch.setattr(backup, "_offline_smoke", inspect)
    assert backup._smoke(snapshot) == {"test": "scratch_isolated"}
    assert backup.tempfile.tempdir == previous and all(not p.exists() for p in scratch)


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
