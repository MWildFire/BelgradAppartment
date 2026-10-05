"""Real synthetic SQLite migration; isolated files and intercepted service commands."""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from alembic import command
from scripts import release
from scripts.rehearse_validation_release import config, seed, snapshot


@pytest.fixture
def first_transition(tmp_path, monkeypatch):
    root = tmp_path / "app"
    (root / "data/state").mkdir(parents=True)
    db = root / "data/otz.db"
    cfg = config(db)
    command.upgrade(cfg, "0014_group_availability_index")
    seed(db)
    (root / "data/photo.jpg").write_bytes(b"synthetic new photo")
    candidate, baseline, legacy = "c" * 40, "b" * 40, "a" * 40
    for name in release.RUNTIME:
        path = root / name
        if "." in name:
            path.write_text("legacy")
        else:
            path.mkdir()
            (path / "legacy.txt").write_text("legacy")
    (root / "data/districts.yaml").write_text("legacy districts")
    (root / ".venv").mkdir()
    release.write_json(root / "release.json", {"revision": legacy})
    packet = tmp_path / "candidate.tar.gz"
    fallback_packet = tmp_path / "fallback.tar.gz"
    packet.touch()
    fallback_packet.touch()
    backup = tmp_path / "backup.json"
    release.write_json(backup, {
        "result": "data_restore_verified", "created_at": datetime.now(UTC).isoformat(),
        "archive_sha256": "a" * 64, "offhost_verified": True,
        "protected_recovery_verified": True,
        "database": {"revisions": ["0014_group_availability_index"]},
        "application": {"catalog": 200, "apartment": 200, "photo": 200, "photo_decoded": True},
    })
    stage = root / ".releases" / candidate
    units = tmp_path / "units"
    units.mkdir()
    timer, service = "otz-nightly-refresh.timer", "otz-site"
    active = {timer, service}
    events = []
    ctx = SimpleNamespace(root=root, db=db, cfg=cfg, stage=stage, events=events, active=active,
                          candidate=candidate, baseline=baseline, legacy=legacy,
                          failure="health", fallback_failure=None, expected=None, units=units)
    ctx.fallback_controller = None
    ctx.args = argparse.Namespace(remote_root=root, uv_bin=tmp_path / "uv", packet=packet,
                                  fallback_packet=fallback_packet, backup_receipt=backup)
    ctx.args.uv_bin.touch()

    def unpack(source, destination):
        revision = baseline if source == fallback_packet else candidate
        code = destination / "code"
        for relative in (*release.RUNTIME, "data/districts.yaml"):
            origin, target = release.ROOT / relative, code / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if origin.is_dir():
                shutil.copytree(origin, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(origin, target)
        (code / "src/build.txt").write_text(revision)
        if revision == baseline and ctx.fallback_controller is not None:
            (code / "scripts/release.py").write_text(ctx.fallback_controller)
        manifest = {
            "revision": revision,
            "tests": {"revision": revision, "result": "passed",
                      "checks": dict.fromkeys(release.CHECKS, "passed")},
            "files": {str(p.relative_to(code)): release.hash_bytes(p.read_bytes())
                      for p in code.rglob("*") if p.is_file()},
        }
        if revision == baseline and ctx.fallback_failure == "missing_check":
            manifest["tests"]["checks"].pop("browser")
        if revision == baseline and ctx.fallback_failure == "tampered":
            (code / "src/matching/matcher.py").write_text("tampered")
        return manifest

    def environment(path, uv):
        (path / "venv/bin").mkdir(parents=True)
        events.append("environment:" + path.name)
        return path / "venv/bin/python"

    def heads(python, code):
        if "fallback" in code.parts and ctx.fallback_failure == "heads":
            return ["0015_durable_validation"]
        return ["0016_identity_edges"]

    def run(args, **kwargs):
        events.append(args)
        if args[:2] == ["systemctl", "stop"]:
            active.difference_update(args[2:])
        if args[:2] == ["systemctl", "start"]:
            active.update(args[2:])
        if args[1:] == ["upgrade", "head"]:
            if ctx.failure == "before_migration":
                raise release.ReleaseError("synthetic migration start failure")
            command.upgrade(cfg, "head")
            with sqlite3.connect(db) as con:
                con.execute("""INSERT INTO validation_jobs(inbound_id,state,reason_code,attempts,
                    outcomes,updated_at) VALUES(2,'pending','provider_unavailable',2,'[]',
                    '2026-10-05')""")
                con.execute("""INSERT INTO digest_work_items(id,source_id,message_id,share_id,
                    payload,post_date,state,attempts,reason_code,lease_token,lease_until)
                    VALUES(1,1,41,'synthetic','{}','2026-10-05','processing',1,'',
                    'synthetic-lease','2026-10-06')""")
                con.execute("""INSERT INTO apartment_identities(id,anchor_listing_id,created_at)
                    VALUES(1,1,'2026-10-05')""")
                con.execute("""INSERT INTO listing_identity_edges
                    (left_id,right_id,state,evidence,created_at)
                    VALUES(1,2,'confirmed','{}','2026-10-05')""")
                con.execute("""INSERT INTO identity_events(id,kind,payload,created_at)
                    VALUES(1,'confirm','{}','2026-10-05')""")
                con.execute("UPDATE source_channels SET last_processed_message_id=41")
            ctx.expected = snapshot(db)
            if ctx.failure == "interrupted_migration":
                raise KeyboardInterrupt("synthetic interruption after migration commit")
        return ""

    def health(*args):
        marker = release.read_json(root / "release.json")["revision"]
        events.append("health:" + marker)
        if ctx.failure == "health" and marker != baseline:
            raise release.ReleaseError("synthetic candidate health failure")
        if ctx.fallback_failure == "health":
            raise release.ReleaseError("synthetic fallback health failure")
        return {"healthz": 200, "readyz": 200}

    real_require = release.require

    def isolated_require(condition, message):
        if message not in {"canonical units target /opt/otz; activation requires root",
                           "unsupported rollback target"} and not message.startswith(
                               "canonical units target"):
            real_require(condition, message)

    monkeypatch.setattr(release, "SYSTEMD_UNITS", units)
    monkeypatch.setattr(release.os, "geteuid", lambda: 0)
    monkeypatch.setattr(release, "require", isolated_require)
    monkeypatch.setattr(release, "unpack_packet", unpack)
    monkeypatch.setattr(release, "stage_runtime_environment", environment)
    monkeypatch.setattr(release, "staged_heads", heads)
    monkeypatch.setattr(release, "run", run)
    monkeypatch.setattr(release, "active", active.__contains__)
    monkeypatch.setattr(release, "service_state", lambda: {"services": [service], "timers": [timer]})
    monkeypatch.setattr(release, "maintenance_idle", lambda: None)
    monkeypatch.setattr(release, "drain_maintenance", lambda: None)
    monkeypatch.setattr(release, "effective_units", lambda deploy: {"synthetic": "checked"})
    monkeypatch.setattr(release, "wait_for_http_health", health)
    return ctx


def resume_with_saved_controller(ctx, monkeypatch):
    saved = release.read_json(ctx.stage / "activation.json")
    controller_path = Path(saved["rollback_command"][1])
    assert controller_path == ctx.stage / "control/release.py"
    assert release.hash_bytes(controller_path.read_bytes()) == saved["control_sha256"]
    spec = importlib.util.spec_from_file_location("isolated_release_control", controller_path)
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    # Load the saved executable from disk. Only host operations/target guard are replaced.
    for name in ("SYSTEMD_UNITS", "run", "active", "drain_maintenance", "effective_units",
                 "wait_for_http_health", "require"):
        monkeypatch.setattr(control, name, getattr(release, name))
    monkeypatch.setattr(sys, "argv", [str(controller_path), *saved["rollback_command"][2:]])
    control.main()


def assert_preserved(ctx):
    assert snapshot(ctx.db) == ctx.expected
    assert (ctx.root / "data/photo.jpg").read_bytes() == b"synthetic new photo"
    assert (ctx.stage / "rollback/src/legacy.txt").read_text() == "legacy"
    assert (ctx.stage / "fallback/code/src/build.txt").read_text() == ctx.baseline


@pytest.mark.parametrize("process_umask", [0o022, 0o077])
def test_first_migration_failure_installs_distinct_fallback_without_losing_new_events(
    first_transition, process_umask,
):
    ctx = first_transition
    old_umask = release.os.umask(process_umask)
    try:
        with pytest.raises(release.ReleaseError, match="candidate health"):
            release.activate(ctx.args)
    finally:
        release.os.umask(old_umask)
    assert (ctx.stage / "fallback").stat().st_mode & 0o777 == 0o750
    state = release.read_json(ctx.stage / "activation.json")
    assert state["result"] == "rolled_back_to_compatible_fallback"
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.baseline
    assert (ctx.root / ".venv").resolve() == ctx.stage / "fallback/venv"
    assert state["fallback_effective_units"] == {"synthetic": "checked"}
    assert ctx.events[-1] == ["systemctl", "start", "otz-nightly-refresh.timer"]
    assert_preserved(ctx)


def test_failure_before_migration_restores_actual_legacy_runtime(first_transition):
    ctx = first_transition
    ctx.failure = "before_migration"
    before = ctx.db.read_bytes()
    with pytest.raises(release.ReleaseError, match="migration start"):
        release.activate(ctx.args)
    assert release.read_json(ctx.stage / "activation.json")["result"] == "rolled_back_code_only"
    assert (ctx.root / "src/legacy.txt").read_text() == "legacy"
    assert ctx.db.read_bytes() == before
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.legacy


@pytest.mark.parametrize("failure", ["missing_check", "tampered", "heads"])
def test_bad_fallback_rejected_before_stopping_writers(first_transition, failure):
    ctx = first_transition
    ctx.fallback_failure = failure
    with pytest.raises(release.ReleaseError):
        release.activate(ctx.args)
    assert ctx.active == {"otz-site", "otz-nightly-refresh.timer"}
    assert not any(isinstance(event, list) and event[0] == "systemctl" for event in ctx.events)
    assert release.current_revisions(ctx.db) == ["0014_group_availability_index"]
    assert (ctx.root / "src/legacy.txt").read_text() == "legacy"


def test_fallback_health_failure_stops_writers_and_can_resume_after_marker(first_transition, monkeypatch):
    ctx = first_transition
    ctx.fallback_failure = "health"
    # The separately tested older baseline does not contain this controller implementation.
    ctx.fallback_controller = "raise SystemExit('older runtime has no first-transition recovery')\n"
    with pytest.raises(release.ReleaseError):
        release.activate(ctx.args)
    assert not ctx.active
    assert release.read_json(ctx.stage / "activation.json")["result"] == "rollback_fallback_failed"
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.baseline
    assert_preserved(ctx)
    ctx.fallback_failure = None
    assert (ctx.root / "scripts/release.py").read_text() == ctx.fallback_controller
    resume_with_saved_controller(ctx, monkeypatch)
    assert release.read_json(ctx.stage / "activation.json")["result"] == "rolled_back_to_compatible_fallback"
    assert_preserved(ctx)


def test_interruption_after_migration_commit_resumes_from_saved_controller(first_transition, monkeypatch):
    ctx = first_transition
    ctx.failure = "interrupted_migration"
    with pytest.raises(KeyboardInterrupt):
        release.activate(ctx.args)
    assert not ctx.active
    assert release.read_json(ctx.stage / "activation.json")["result"] == "activating"
    assert release.current_revisions(ctx.db) == ["0016_identity_edges"]
    resume_with_saved_controller(ctx, monkeypatch)
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.baseline
    assert_preserved(ctx)


@pytest.mark.parametrize("relative", ["src", "alembic", ".venv"])
@pytest.mark.parametrize("point", ["after_old_rename", "after_new_rename"])
def test_interrupted_candidate_swap_restores_complete_legacy_runtime(
    first_transition, monkeypatch, relative, point,
):
    ctx = first_transition
    actual_replace = release.os.replace
    fired = []

    def interrupt(source, target):
        source, target = Path(source), Path(target)
        result = actual_replace(source, target)
        expected = (ctx.stage / "rollback" / relative if point == "after_old_rename"
                    else ctx.root / relative)
        if target == expected and not fired:
            fired.append(True)
            raise KeyboardInterrupt("synthetic candidate swap interruption")
        return result

    monkeypatch.setattr(release.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        release.activate(ctx.args)
    assert fired and not ctx.active
    monkeypatch.setattr(release.os, "replace", actual_replace)
    resume_with_saved_controller(ctx, monkeypatch)
    assert release.current_revisions(ctx.db) == ["0014_group_availability_index"]
    assert release.read_json(ctx.stage / "activation.json")["result"] == "rolled_back_code_only"
    for path in release.RUNTIME[:4]:
        assert (ctx.root / path / "legacy.txt").read_text() == "legacy"
    assert (ctx.root / ".venv").is_dir() and not (ctx.root / ".venv").is_symlink()
    assert ctx.active == {"otz-site", "otz-nightly-refresh.timer"}


def test_repeated_interruptions_preserve_legacy_path_without_rejected_candidate(
    first_transition, monkeypatch,
):
    ctx = first_transition
    actual_replace = release.os.replace
    boundaries = [ctx.stage / "rollback/alembic", ctx.root / "alembic"]

    def interrupt(source, target):
        result = actual_replace(source, target)
        if boundaries and Path(target) == boundaries[0]:
            boundaries.pop(0)
            raise KeyboardInterrupt("synthetic repeated process interruption")
        return result

    monkeypatch.setattr(release.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        release.activate(ctx.args)
    with pytest.raises(KeyboardInterrupt):
        resume_with_saved_controller(ctx, monkeypatch)
    assert not boundaries and not ctx.active
    assert not (ctx.stage / "rejected/alembic").exists()
    monkeypatch.setattr(release.os, "replace", actual_replace)
    resume_with_saved_controller(ctx, monkeypatch)
    for relative in release.RUNTIME[:4]:
        assert (ctx.root / relative / "legacy.txt").read_text() == "legacy"
    assert ctx.active == {"otz-site", "otz-nightly-refresh.timer"}


@pytest.mark.parametrize("point", ["after_old_rename", "after_new_rename"])
def test_interrupted_activation_marker_resumes_from_saved_controller(first_transition, monkeypatch, point):
    ctx = first_transition
    ctx.failure = None
    actual_replace = release.os.replace
    fired = []

    def interrupt(source, target):
        source, target = Path(source), Path(target)
        result = actual_replace(source, target)
        old_boundary = target == ctx.stage / "rollback/release.json"
        new_boundary = (source == ctx.stage / "new-release.json"
                        and target == ctx.root / "release.json")
        if (old_boundary if point == "after_old_rename" else new_boundary) and not fired:
            fired.append(True)
            raise KeyboardInterrupt("synthetic activation marker interruption")
        return result

    monkeypatch.setattr(release.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        release.activate(ctx.args)
    assert fired and "otz-nightly-refresh.timer" not in ctx.active
    monkeypatch.setattr(release.os, "replace", actual_replace)
    resume_with_saved_controller(ctx, monkeypatch)
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.baseline
    assert_preserved(ctx)


@pytest.mark.parametrize("revision", ["0015_validation_jobs", "unknown_revision"])
def test_partial_or_unknown_schema_never_starts_legacy_or_fallback(first_transition, revision):
    ctx = first_transition
    ctx.failure = None
    release.activate(ctx.args)
    with sqlite3.connect(ctx.db) as con:
        con.execute("UPDATE alembic_version SET version_num=?", (revision,))
    with pytest.raises(release.ReleaseError, match="incompatible"):
        release.restore_code(ctx.root, ctx.stage)
    assert not ctx.active
    assert (ctx.root / "src/build.txt").read_text() == ctx.candidate


@pytest.mark.parametrize("point", ["after_old_rename", "after_new_rename"])
@pytest.mark.parametrize("relative", ["src", ".venv", "release.json"])
def test_interrupted_fallback_swap_resumes_without_overwriting_archives(
    first_transition, monkeypatch, point, relative,
):
    ctx = first_transition
    actual_replace = release.os.replace
    fired = []

    def interrupt(source, target):
        source, target = Path(source), Path(target)
        value = actual_replace(source, target)
        expected = (ctx.stage / "fallback-rejected" / relative if point == "after_old_rename"
                    else ctx.root / relative)
        fallback_move = (point == "after_old_rename"
                         or source.is_relative_to(ctx.stage / "fallback-install"))
        if target == expected and fallback_move and not fired:
            fired.append(True)
            raise KeyboardInterrupt("synthetic process interruption")
        return value

    monkeypatch.setattr(release.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        release.activate(ctx.args)
    assert fired and not ctx.active
    monkeypatch.setattr(release.os, "replace", actual_replace)
    resume_with_saved_controller(ctx, monkeypatch)
    assert release.read_json(ctx.stage / "activation.json")["result"] == "rolled_back_to_compatible_fallback"
    assert (ctx.stage / "fallback-rejected/src/build.txt").read_text() == ctx.candidate
    assert_preserved(ctx)
