"""Opt-in DB+media release is narrower than the unchanged default protected gate."""

import hashlib
import json
import shutil
from datetime import UTC, datetime

import pytest

from alembic import command
from scripts import release
from scripts.rehearse_validation_release import snapshot
from tests.test_release_first_transition import first_transition, resume_with_saved_controller


def receipt():
    return {
        "result": "data_restore_verified", "created_at": datetime.now(UTC).isoformat(),
        "backup_format": "data_only_directory_generation", "backup_scope": "db_media",
        "generation_id": "20261006-090000-123abc45", "manifest_sha256": "a" * 64,
        "generation_manifest_sha256": "a" * 64, "offhost_verified": True,
        "protected_recovery_verified": False, "source_revision": "a" * 40,
        "credential_inspection": {"result": "heuristic_scan_passed"},
        "source_snapshot_immutable": True, "media_decode_verified": True,
        "media_files_verified": 1, "database": {"revisions": ["0016_identity_edges"]},
        "application": {"catalog": 200, "apartment": 200, "photo": 200,
                        "photo_decoded": True, "network": "disabled"},
    }


def test_data_only_proof_cannot_unlock_default_release():
    with pytest.raises(release.ReleaseError, match="explicit code-only"):
        release.validate_backup(receipt())
    release.validate_backup(receipt(), allow_data_only=True)


@pytest.mark.parametrize("change", [
    {"backup_scope": "anything"}, {"credential_inspection": {}},
    {"protected_recovery_verified": True}, {"offhost_verified": False},
    {"source_snapshot_immutable": False}, {"media_decode_verified": False},
    {"media_files_verified": 0}, {"source_revision": "unknown"},
    {"application": {"catalog": 200, "apartment": 200, "photo": 200,
                     "photo_decoded": True, "network": "enabled"}},
])
def test_incomplete_data_only_proof_stays_blocked(change):
    with pytest.raises(release.ReleaseError):
        release.validate_backup(receipt() | change, allow_data_only=True)


@pytest.fixture
def current(tmp_path):
    files = {}
    for name in ("pyproject.toml", "uv.lock", "alembic.ini", "src/config.py", "src/db/models.py",
                 "scripts/runtime_contract.json", "data/districts.yaml", "alembic/versions/one.py",
                 "deploy/otz-monitor.service", "src/scrapers/orchestrator.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original\n")
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / "release.json").write_text(json.dumps({"revision": "a" * 40, "files": files}))
    return tmp_path, {"revision": "b" * 40, "files": files}


def test_diagnostic_code_delta_permitted_with_current_runtime_as_rollback(current):
    root, candidate = current
    candidate["files"]["src/scrapers/orchestrator.py"] = "b" * 64
    release.validate_data_only_release(root, candidate, receipt())


@pytest.mark.parametrize("name", ["uv.lock", "src/config.py", "src/db/models.py",
                                 "alembic/versions/one.py", "deploy/otz-monitor.service"])
def test_configuration_migration_and_dependency_changes_rejected(current, name):
    root, candidate = current
    candidate["files"][name] = "b" * 64
    with pytest.raises(release.ReleaseError, match="forbids schema"):
        release.validate_data_only_release(root, candidate, receipt())


def test_live_drift_or_different_backup_source_rejected(current):
    root, candidate = current
    with pytest.raises(release.ReleaseError, match="different source release"):
        release.validate_data_only_release(root, candidate, receipt() | {"source_revision": "c" * 40})
    (root / "src/config.py").write_text("unrecorded change")
    with pytest.raises(release.ReleaseError, match="live schema"):
        release.validate_data_only_release(root, candidate, receipt())


@pytest.fixture
def code_only_transition(tmp_path, monkeypatch):
    ctx = first_transition.__wrapped__(tmp_path, monkeypatch)
    staged = tmp_path / "current-runtime"
    manifest = release.unpack_packet(ctx.args.fallback_packet, staged)
    for relative in (*release.RUNTIME, "data/districts.yaml"):
        target = ctx.root / relative
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        shutil.move(str(staged / "code" / relative), target)
    release.write_json(ctx.root / "release.json", manifest)
    command.upgrade(ctx.cfg, "head")
    ctx.expected = snapshot(ctx.db)
    ctx.args.fallback_packet = None
    ctx.args.code_only_data_backup = True
    release.write_json(ctx.args.backup_receipt, receipt() | {"source_revision": ctx.baseline})
    ctx.failure = None
    return ctx


def test_code_only_activation_and_saved_controller_rollback_keep_database(
    code_only_transition, monkeypatch,
):
    ctx = code_only_transition
    before = ctx.db.read_bytes()
    state = release.activate(ctx.args)
    assert state["code_only"] is True
    assert state["protected_recovery_verified"] is False
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.candidate
    assert ctx.db.read_bytes() == before
    assert snapshot(ctx.db) == ctx.expected
    assert not any(isinstance(event, list) and event[1:] == ["upgrade", "head"]
                   for event in ctx.events)
    resume_with_saved_controller(ctx, monkeypatch)
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.baseline
    assert release.read_json(ctx.stage / "activation.json")["result"] == "rolled_back_code_only"
    assert ctx.db.read_bytes() == before
    assert ctx.active == {"otz-site", "otz-nightly-refresh.timer"}


def test_code_only_failed_health_automatically_restores_current_runtime(
    code_only_transition, monkeypatch,
):
    ctx = code_only_transition
    before = ctx.db.read_bytes()

    def health(*args):
        if (ctx.root / "src/build.txt").read_text() == ctx.candidate:
            raise release.ReleaseError("synthetic candidate health failure")
        return {"healthz": 200, "readyz": 200}

    monkeypatch.setattr(release, "wait_for_http_health", health)
    with pytest.raises(release.ReleaseError, match="candidate health"):
        release.activate(ctx.args)
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.baseline
    assert release.read_json(ctx.stage / "activation.json")["result"] == "rolled_back_code_only"
    assert ctx.db.read_bytes() == before
    assert ctx.active == {"otz-site", "otz-nightly-refresh.timer"}


def test_code_only_target_schema_mismatch_blocks_before_stopping_services(
    code_only_transition, monkeypatch,
):
    ctx = code_only_transition
    monkeypatch.setattr(release, "staged_heads", lambda *args: ["different_schema"])
    with pytest.raises(release.ReleaseError, match="forbids database migrations"):
        release.activate(ctx.args)
    assert not any(isinstance(event, list) and event[0] == "systemctl" for event in ctx.events)
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.baseline
