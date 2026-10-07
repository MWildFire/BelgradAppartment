"""Synthetic v1 backup attestation: explicit loss consent, unchanged code-only gates."""

import importlib.util
import json
import shlex
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import release
from tests import test_release_data_only as ordinary


def receipt():
    proof = ordinary.receipt()
    proof.update(
        backup_format="sanitized_directory_generation", backup_scope="db_media_sanitized",
        files=149, files_transferred=149, media_files_verified=147,
        media_reused_from_verified_seed=0, source_temporary_snapshot_removed=True,
        health_receipt_published=False, personal_data_present=True,
        credential_inspection={"result": "heuristic_scan_passed", "cells_scanned": 7,
                               "bytes_scanned": 700, "arbitrary_secrets_absence_proven": False},
        affected_albums={"listings": 5, "photos": 147, "markers": 43},
        listing_albums={"listings": 5, "photos": 147, "markers": 43, "network": "disabled"},
        sanitization={
            "format": "renfinder-db-media-sanitized-v1", "scope": "db_media_sanitized",
            "source_revision": proof["source_revision"], "revisions": ["0016_identity_edges"],
            "source_database_modified": False, "other_database_values_unchanged": True,
            "marker_scheme": "restore-local", "array_positions_preserved": True,
            "original_urls_retained": False, "protected_recovery_verified": False,
            "sqlite_journal_mode": "delete", "sqlite_freelist_count": 0,
            "database_sidecars": [],
            "credential_inspection": {"result": "heuristic_scan_passed", "scanner_exemptions": []},
            "redaction": {"affected_cells": 5, "affected_photo_slots": 147,
                          "halo_cells": 2, "halo_urls": 40, "resizer_cells": 3,
                          "resizer_urls": 3, "remaining_original_urls": 104,
                          "replaced_url_values": 43},
        },
    )
    return proof


def test_sanitized_restore_requires_both_explicit_opt_ins():
    for options in ({}, {"allow_data_only": True}, {"allow_sanitized": True}):
        with pytest.raises(release.ReleaseError):
            release.validate_backup(receipt(), **options)
    release.validate_backup(receipt(), allow_data_only=True, allow_sanitized=True)
    with pytest.raises(release.ReleaseError, match="requires a sanitized receipt"):
        release.validate_backup(ordinary.receipt(), allow_data_only=True, allow_sanitized=True)


def test_sanitized_proof_cannot_be_relabelled_as_ordinary_data_only():
    proof = receipt()
    proof.update(backup_format="data_only_directory_generation", backup_scope="db_media")
    with pytest.raises(release.ReleaseError, match="must not be relabelled"):
        release.validate_backup(proof, allow_data_only=True)


def test_album_restore_proof_requires_enough_verified_media():
    proof = receipt()
    proof.update(media_files_verified=1, files=3, files_transferred=3)
    with pytest.raises(release.ReleaseError):
        release.validate_backup(proof, allow_data_only=True, allow_sanitized=True)


@pytest.mark.parametrize(("path", "value"), [
    (("backup_scope",), "db_media"), (("protected_recovery_verified",), True),
    (("offhost_verified",), False), (("source_snapshot_immutable",), False),
    (("source_temporary_snapshot_removed",), False), (("media_decode_verified",), False),
    (("media_files_verified",), True), (("files_transferred",), 148),
    (("files",), 150), (("media_reused_from_verified_seed",), 1),
    (("health_receipt_published",), True), (("personal_data_present",), False),
    (("generation_manifest_sha256",), "b" * 64), (("generation_id",), "not-a-generation"),
    (("application", "network"), "enabled"), (("application", "photo_decoded"), False),
    (("application", "catalog"), 500), (("listing_albums", "photos"), 146),
    (("affected_albums", "markers"), 42), (("database", "revisions"), ["0015_validation_work"]),
    (("credential_inspection", "arbitrary_secrets_absence_proven"), True),
    (("credential_inspection", "cells_scanned"), True), (("credential_inspection",), []),
    (("sanitization", "source_revision"), "b" * 40),
    (("sanitization", "source_database_modified"), True),
    (("sanitization", "other_database_values_unchanged"), False),
    (("sanitization", "array_positions_preserved"), 1),
    (("sanitization", "marker_scheme"), "https"),
    (("sanitization", "original_urls_retained"), True),
    (("sanitization", "sqlite_freelist_count"), False),
    (("sanitization", "sqlite_journal_mode"), "wal"),
    (("sanitization", "database_sidecars"), ["synthetic-wal"]),
    (("sanitization", "redaction", "halo_urls"), 39),
    (("sanitization", "redaction", "remaining_original_urls"), 103),
    (("sanitization", "credential_inspection", "scanner_exemptions"), ["skip"]),
    (("sanitization", "unreviewed_extension"), True),
])
def test_incomplete_or_changed_loss_contract_fails_closed(path, value):
    proof = receipt()
    part = proof
    for key in path[:-1]:
        part = part[key]
    part[path[-1]] = value
    with pytest.raises(release.ReleaseError):
        release.validate_backup(proof, allow_data_only=True, allow_sanitized=True)


def test_sanitized_mode_does_not_extend_backup_freshness():
    proof = receipt()
    proof["created_at"] = (datetime.now(UTC) - timedelta(hours=37)).isoformat()
    with pytest.raises(release.ReleaseError, match="no older than 36 hours"):
        release.validate_backup(proof, allow_data_only=True, allow_sanitized=True)


@pytest.fixture
def transition(tmp_path, monkeypatch):
    ctx = ordinary.code_only_transition.__wrapped__(tmp_path, monkeypatch)
    proof = receipt()
    proof["source_revision"] = ctx.baseline
    proof["sanitization"]["source_revision"] = ctx.baseline
    release.write_json(ctx.args.backup_receipt, proof)
    ctx.args.acknowledge_sanitized_url_loss = True
    return ctx


def test_sanitized_activation_records_scope_and_saved_rollback_keeps_live_db(
    transition, monkeypatch,
):
    ordinary.test_code_only_activation_and_saved_controller_rollback_keep_database(
        transition, monkeypatch,
    )
    state = release.read_json(transition.stage / "activation.json")
    assert state["backup_scope"] == "db_media_sanitized"
    assert state["sanitized_url_loss_acknowledged"] is True
    assert state["protected_recovery_verified"] is False


def test_sanitized_failed_health_restores_prior_code_without_restoring_data(
    transition, monkeypatch,
):
    ordinary.test_code_only_failed_health_automatically_restores_current_runtime(
        transition, monkeypatch,
    )


def test_sanitized_target_schema_mismatch_stops_before_services(transition, monkeypatch):
    ordinary.test_code_only_target_schema_mismatch_blocks_before_stopping_services(
        transition, monkeypatch,
    )


@pytest.mark.parametrize("change", ["consent", "code_only", "source", "config", "schema"])
def test_sanitized_rejection_precedes_service_changes(transition, change):
    ctx = transition
    if change == "consent":
        ctx.args.acknowledge_sanitized_url_loss = False
    elif change == "code_only":
        ctx.args.code_only_data_backup = False
    elif change == "source":
        proof = release.read_json(ctx.args.backup_receipt)
        proof["source_revision"] = proof["sanitization"]["source_revision"] = "c" * 40
        release.write_json(ctx.args.backup_receipt, proof)
    elif change == "config":
        (ctx.root / "src/config.py").write_text("unreviewed drift\n")
    else:
        proof = release.read_json(ctx.args.backup_receipt)
        proof["database"]["revisions"] = ["0015_validation_work"]
        release.write_json(ctx.args.backup_receipt, proof)
    before = ctx.db.read_bytes()
    with pytest.raises(release.ReleaseError):
        release.activate(ctx.args)
    assert ctx.db.read_bytes() == before
    assert not any(isinstance(event, list) and event[0] == "systemctl" for event in ctx.events)


@pytest.mark.parametrize("acknowledge", [False, True])
def test_transport_rejects_missing_consent_before_ssh_and_forwards_opt_in(
    tmp_path, monkeypatch, acknowledge,
):
    monkeypatch.setitem(sys.modules, "release", release)
    spec = importlib.util.spec_from_file_location(
        "synthetic_deploy_packet", Path(release.__file__).with_name("deploy_packet.py"),
    )
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt()))
    argv = ["deploy", "--target", "synthetic@example.invalid", "--remote-root", "/opt/otz",
            "--packet", str(tmp_path / "synthetic.tar.gz"), "--backup-receipt", str(path),
            "--uv-bin", "/synthetic/uv", "--code-only-data-backup", "--activate"]
    if acknowledge:
        argv.append("--acknowledge-sanitized-url-loss")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(deploy, "unpack_packet", lambda *args: {
        "revision": "b" * 40,
        "tests": {"revision": "b" * 40, "result": "passed",
                  "checks": dict.fromkeys(release.CHECKS, "passed")},
    })
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="/tmp/otz-release.SYNTHETIC\n")

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    if not acknowledge:
        with pytest.raises(release.ReleaseError, match="43 lost URLs"):
            deploy.main()
        assert calls == []
    else:
        deploy.main()
        assert calls[0][0] == "ssh" and calls[-1][0] == "ssh"
        assert "--acknowledge-sanitized-url-loss" in shlex.split(calls[-1][-1])
        assert "--code-only-data-backup" in shlex.split(calls[-1][-1])
