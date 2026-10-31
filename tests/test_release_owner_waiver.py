"""Explicit owner waiver of the pre-release backup: narrower than the receipt path.

The waiver unlocks only the *backup receipt* requirement of the existing code-only route.
Every file/schema gate of that route must keep working without a receipt.
"""

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import release
from tests import test_release_data_only as data_only
from tests.test_release_data_only import receipt
from tests.test_release_first_transition import resume_with_saved_controller

REVISION = "c" * 40


@pytest.fixture
def transition(tmp_path, monkeypatch):
    """The code-only activation scenario of test_release_data_only, rebuilt for the waiver."""
    return data_only.code_only_transition.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def live(tmp_path):
    """Live release files plus a candidate manifest, as in test_release_data_only."""
    return data_only.current.__wrapped__(tmp_path)


def waiver(revision=REVISION, **change):
    value = {
        "kind": "owner_waiver_no_backup",
        "owner": "Dmitry",
        "quote": "Roll out without a production backup",
        "given_at": datetime.now(UTC).isoformat(),
        "revision": revision,
    }
    return value | change


# --- the waiver document ------------------------------------------------------------


def test_valid_owner_waiver_is_accepted():
    release.validate_owner_waiver(waiver(), REVISION)


def test_cyrillic_quote_survives_the_json_round_trip(tmp_path):
    path = tmp_path / "waiver.json"
    quote = "Выкатывай без копии прода"
    release.write_json(path, waiver(quote=quote))
    loaded = release.read_json(path)
    release.validate_owner_waiver(loaded, REVISION)
    assert loaded["quote"] == quote


def test_utc_z_suffix_and_small_clock_skew_are_accepted():
    stamp = (datetime.now(UTC) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    release.validate_owner_waiver(waiver(given_at=stamp), REVISION)


def test_waiver_just_inside_24_hours_is_accepted():
    given = (datetime.now(UTC) - timedelta(hours=23, minutes=50)).isoformat()
    release.validate_owner_waiver(waiver(given_at=given), REVISION)


@pytest.mark.parametrize(("change", "message"), [
    ({"kind": "owner_waiver"}, "kind"),
    ({"kind": None}, "kind"),
    ({"owner": ""}, "owner"),
    ({"owner": "   "}, "owner"),
    ({"owner": 7}, "owner"),
    ({"quote": ""}, "quote"),
    ({"quote": "  \n"}, "quote"),
    ({"quote": None}, "quote"),
    ({"revision": "d" * 40}, "different release revision"),
    ({"revision": "C" * 40}, "revision"),
    ({"revision": "c" * 39}, "revision"),
    ({"revision": None}, "revision"),
    ({"given_at": "not a time"}, "given_at"),
    ({"given_at": None}, "given_at"),
    ({"given_at": datetime.now(UTC).replace(tzinfo=None).isoformat()}, "timezone"),
    ({"given_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat()}, "older than 24 hours"),
    ({"given_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat()}, "in the future"),
])
def test_invalid_owner_waiver_is_rejected(change, message):
    with pytest.raises(release.ReleaseError, match=message):
        release.validate_owner_waiver(waiver(**change), REVISION)


@pytest.mark.parametrize("missing", ["kind", "owner", "quote", "given_at", "revision"])
def test_waiver_with_missing_field_is_rejected(missing):
    value = waiver()
    del value[missing]
    with pytest.raises(release.ReleaseError, match="fields"):
        release.validate_owner_waiver(value, REVISION)


def test_waiver_with_unknown_field_or_wrong_container_is_rejected():
    with pytest.raises(release.ReleaseError, match="fields"):
        release.validate_owner_waiver(waiver(extra="x"), REVISION)
    with pytest.raises(release.ReleaseError, match="object"):
        release.validate_owner_waiver([waiver()], REVISION)


# --- choosing between receipt and waiver --------------------------------------------


def choice(backup=None, owner=None, **change):
    options = {"code_only": True, "sanitized_ack": False, "fallback": False} | change
    return release.check_backup_choice(backup, owner, **options)


def test_backup_choice_requires_exactly_one_of_receipt_and_waiver():
    choice(backup={"x": 1})
    choice(owner={"x": 1})
    with pytest.raises(release.ReleaseError, match="exactly one"):
        choice()
    with pytest.raises(release.ReleaseError, match="exactly one"):
        choice(backup={"x": 1}, owner={"x": 1})


def test_waiver_needs_code_only_and_forbids_sanitized_ack_and_fallback():
    with pytest.raises(release.ReleaseError, match="code-only"):
        choice(owner={"x": 1}, code_only=False)
    with pytest.raises(release.ReleaseError, match="sanitized"):
        choice(owner={"x": 1}, sanitized_ack=True)
    with pytest.raises(release.ReleaseError, match="fallback"):
        choice(owner={"x": 1}, fallback=True)


def test_receipt_path_keeps_its_existing_flag_combinations():
    choice(backup={"x": 1}, code_only=False)
    choice(backup={"x": 1}, sanitized_ack=True)
    choice(backup={"x": 1}, fallback=True, code_only=False)


# --- file and schema gates without a receipt ----------------------------------------


def test_data_only_gate_without_receipt_accepts_diagnostic_code_delta(live):
    root, candidate = live
    candidate["files"]["src/scrapers/orchestrator.py"] = "b" * 64
    # The live release.json names another source revision; with no receipt that is not a gate.
    release.validate_data_only_release(root, candidate, owner_waiver=True)


@pytest.mark.parametrize("name", [
    "pyproject.toml", "uv.lock", "alembic.ini", "src/config.py", "src/db/models.py",
    "scripts/runtime_contract.json", "data/districts.yaml", "alembic/versions/one.py",
    "deploy/otz-monitor.service",
])
def test_release_files_still_must_match_without_receipt(live, name):
    root, candidate = live
    candidate["files"][name] = "b" * 64
    with pytest.raises(release.ReleaseError, match="forbids schema"):
        release.validate_data_only_release(root, candidate, owner_waiver=True)


def test_new_deploy_unit_still_rejected_without_receipt(live):
    root, candidate = live
    candidate["files"]["deploy/otz-bot.service"] = "b" * 64
    with pytest.raises(release.ReleaseError, match="forbids schema"):
        release.validate_data_only_release(root, candidate, owner_waiver=True)


def test_live_drift_still_rejected_without_receipt(live):
    root, candidate = live
    (root / "src/config.py").write_text("unrecorded change")
    with pytest.raises(release.ReleaseError, match="live schema"):
        release.validate_data_only_release(root, candidate, owner_waiver=True)


def test_missing_live_release_files_still_rejected_without_receipt(live):
    root, candidate = live
    (root / "uv.lock").unlink()
    with pytest.raises(release.ReleaseError, match="live schema"):
        release.validate_data_only_release(root, candidate, owner_waiver=True)


def test_data_only_gate_never_skips_the_receipt_by_accident(live):
    root, candidate = live
    with pytest.raises(release.ReleaseError, match="receipt"):
        release.validate_data_only_release(root, candidate, None)
    with pytest.raises(release.ReleaseError, match="receipt"):
        release.validate_data_only_release(root, candidate, receipt(), owner_waiver=True)
    # And the receipt path is exactly as strict as before.
    with pytest.raises(release.ReleaseError, match="different source release"):
        release.validate_data_only_release(root, candidate, receipt() | {"source_revision": "c" * 40})


# --- activation ---------------------------------------------------------------------


def use_waiver(ctx, tmp_path, **change):
    path = tmp_path / "owner-waiver.json"
    release.write_json(path, waiver(**({"revision": ctx.candidate} | change)))
    ctx.args.backup_receipt = None
    ctx.args.owner_waiver = path
    return path


def no_host_changes(ctx):
    assert not any(isinstance(event, list) and event[0] == "systemctl" for event in ctx.events)
    assert not ctx.stage.exists()
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.baseline


def test_owner_waiver_activates_code_only_release_and_keeps_database(
    transition, tmp_path,
):
    ctx = transition
    path = use_waiver(ctx, tmp_path)
    # Not even a stale or foreign receipt may be needed on this path.
    ctx.args.backup_receipt = None
    before = ctx.db.read_bytes()
    state = release.activate(ctx.args)
    assert state["code_only"] is True
    assert state["owner_waiver"] is True
    assert state["backup_scope"] == "owner_waiver_no_backup"
    assert state["protected_recovery_verified"] is False
    assert state["sanitized_url_loss_acknowledged"] is False
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.candidate
    assert ctx.db.read_bytes() == before
    assert release.read_json(ctx.stage / "backup-waiver.json") == release.read_json(path)
    assert not (ctx.stage / "backup-receipt.json").exists()
    saved = release.read_json(ctx.stage / "activation.json")
    assert saved["owner_waiver"] is True
    assert saved["backup_scope"] == "owner_waiver_no_backup"
    assert not any(isinstance(event, list) and event[1:] == ["upgrade", "head"]
                   for event in ctx.events)


def test_owner_waiver_release_can_be_rolled_back_with_saved_controller(
    transition, tmp_path, monkeypatch,
):
    ctx = transition
    use_waiver(ctx, tmp_path)
    before = ctx.db.read_bytes()
    release.activate(ctx.args)
    resume_with_saved_controller(ctx, monkeypatch)
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.baseline
    assert release.read_json(ctx.stage / "activation.json")["result"] == "rolled_back_code_only"
    assert ctx.db.read_bytes() == before


def test_receipt_path_is_unchanged(transition):
    ctx = transition
    state = release.activate(ctx.args)
    assert state["backup_scope"] == "db_media"
    assert "owner_waiver" not in state
    assert (ctx.stage / "backup-receipt.json").is_file()
    assert not (ctx.stage / "backup-waiver.json").exists()


def test_owner_waiver_without_code_only_is_refused(transition, tmp_path):
    ctx = transition
    use_waiver(ctx, tmp_path)
    ctx.args.code_only_data_backup = False
    with pytest.raises(release.ReleaseError, match="code-only"):
        release.activate(ctx.args)
    no_host_changes(ctx)


def test_owner_waiver_with_fallback_packet_is_refused(transition, tmp_path):
    ctx = transition
    use_waiver(ctx, tmp_path)
    ctx.args.fallback_packet = tmp_path / "fallback.tar.gz"
    with pytest.raises(release.ReleaseError, match="fallback"):
        release.activate(ctx.args)
    no_host_changes(ctx)


def test_owner_waiver_with_sanitized_acknowledgement_is_refused(transition, tmp_path):
    ctx = transition
    use_waiver(ctx, tmp_path)
    ctx.args.acknowledge_sanitized_url_loss = True
    with pytest.raises(release.ReleaseError, match="sanitized"):
        release.activate(ctx.args)
    no_host_changes(ctx)


def test_owner_waiver_together_with_receipt_is_refused(transition, tmp_path):
    ctx = transition
    receipt_path = ctx.args.backup_receipt
    use_waiver(ctx, tmp_path)
    ctx.args.backup_receipt = receipt_path
    with pytest.raises(release.ReleaseError, match="exactly one"):
        release.activate(ctx.args)
    no_host_changes(ctx)


def test_neither_receipt_nor_waiver_is_refused(transition):
    ctx = transition
    ctx.args.backup_receipt = None
    with pytest.raises(release.ReleaseError, match="exactly one"):
        release.activate(ctx.args)
    no_host_changes(ctx)


@pytest.mark.parametrize(("change", "message"), [
    ({"revision": "d" * 40}, "different release revision"),
    ({"given_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat()}, "older than 24 hours"),
    ({"given_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat()}, "in the future"),
    ({"quote": ""}, "quote"),
    ({"kind": "something_else"}, "kind"),
])
def test_bad_owner_waiver_is_refused_before_any_host_change(
    transition, tmp_path, change, message,
):
    ctx = transition
    use_waiver(ctx, tmp_path, **change)
    with pytest.raises(release.ReleaseError, match=message):
        release.activate(ctx.args)
    no_host_changes(ctx)


def test_owner_waiver_still_forbids_changed_release_files(
    transition, tmp_path, monkeypatch,
):
    ctx = transition
    use_waiver(ctx, tmp_path)
    unpack = release.unpack_packet

    def changed(source, destination):
        manifest = unpack(source, destination)
        manifest["files"]["uv.lock"] = "b" * 64
        return manifest

    monkeypatch.setattr(release, "unpack_packet", changed)
    with pytest.raises(release.ReleaseError, match="forbids schema"):
        release.activate(ctx.args)
    no_host_changes(ctx)


def test_owner_waiver_still_forbids_database_migrations(
    transition, tmp_path, monkeypatch,
):
    ctx = transition
    use_waiver(ctx, tmp_path)
    monkeypatch.setattr(release, "staged_heads", lambda *args: ["different_schema"])
    with pytest.raises(release.ReleaseError, match="forbids database migrations"):
        release.activate(ctx.args)
    assert not any(isinstance(event, list) and event[0] == "systemctl" for event in ctx.events)
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.baseline


def test_owner_waiver_failed_health_restores_current_runtime(
    transition, tmp_path, monkeypatch,
):
    ctx = transition
    use_waiver(ctx, tmp_path)
    before = ctx.db.read_bytes()

    def health(*args):
        if (ctx.root / "src/build.txt").read_text() == ctx.candidate:
            raise release.ReleaseError("synthetic candidate health failure")
        return {"healthz": 200, "readyz": 200}

    monkeypatch.setattr(release, "wait_for_http_health", health)
    with pytest.raises(release.ReleaseError, match="candidate health"):
        release.activate(ctx.args)
    assert release.read_json(ctx.root / "release.json")["revision"] == ctx.baseline
    assert ctx.db.read_bytes() == before


def test_command_line_accepts_waiver_instead_of_receipt(tmp_path, monkeypatch):
    seen = {}

    def activate(args):
        seen["args"] = args
        return {"result": "stub"}

    monkeypatch.setattr(release, "activate", activate)
    base = ["release.py", "activate", "--packet", "p.tar.gz", "--remote-root", "/opt/otz",
            "--uv-bin", "/opt/otz/tools/uv", "--code-only-data-backup"]
    monkeypatch.setattr(release.sys, "argv", [*base, "--owner-waiver", str(tmp_path / "w.json")])
    release.main()
    assert seen["args"].owner_waiver == tmp_path / "w.json"
    assert seen["args"].backup_receipt is None
    monkeypatch.setattr(release.sys, "argv", [*base, "--backup-receipt", str(tmp_path / "b.json")])
    release.main()
    assert seen["args"].owner_waiver is None
    assert seen["args"].backup_receipt == tmp_path / "b.json"


# --- deploy_packet.py ---------------------------------------------------------------


@pytest.fixture
def packet_tool(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(release.__file__).parent))
    tool = importlib.import_module("deploy_packet")
    calls = []
    manifest = {"revision": REVISION,
                "tests": {"revision": REVISION, "result": "passed",
                          "checks": dict.fromkeys(tool.CHECKS, "passed")}}

    def unpack(packet, destination):
        (destination / "code/scripts").mkdir(parents=True)
        (destination / "code/scripts/release.py").write_text("# controller\n")
        return manifest

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="/tmp/otz-release.AbCd1234\n", returncode=0)

    monkeypatch.setattr(tool, "unpack_packet", unpack)
    monkeypatch.setattr(tool.subprocess, "run", run)
    packet = tmp_path / "release.tar.gz"
    packet.touch()
    return SimpleNamespace(tool=tool, calls=calls, packet=packet, tmp=tmp_path, monkeypatch=monkeypatch)


def run_tool(env, *options):
    argv = ["deploy_packet.py", "--target", "ubuntu@203.0.113.9", "--remote-root", "/opt/otz",
            "--packet", str(env.packet), "--uv-bin", "/opt/otz/tools/uv", *options]
    env.monkeypatch.setattr(sys, "argv", argv)
    env.tool.main()


def test_deploy_packet_stages_and_runs_owner_waiver(packet_tool, capsys):
    path = packet_tool.tmp / "owner-waiver.json"
    release.write_json(path, waiver())
    run_tool(packet_tool, "--code-only-data-backup", "--owner-waiver", str(path), "--activate")
    uploads = [call for call in packet_tool.calls if call[0] == "scp"]
    assert [Path(call[-1].split(":", 1)[1]).name for call in uploads] == [
        "packet.tar.gz", "waiver.json", "release.py"]
    assert str(path) in [call[-2] for call in uploads]
    remote = next(call for call in packet_tool.calls if call[0] == "ssh" and "activate" in call[-1])
    assert "--owner-waiver" in remote[-1] and "waiver.json" in remote[-1]
    assert "--backup-receipt" not in remote[-1]
    assert "--code-only-data-backup" in remote[-1]
    printed = json.loads(capsys.readouterr().out)
    assert printed["result"] == "activation_finished"
    assert "--owner-waiver" in printed["activation_command"]


def test_deploy_packet_receipt_path_is_unchanged(packet_tool):
    receipt_path = packet_tool.tmp / "backup.json"
    release.write_json(receipt_path, receipt())
    run_tool(packet_tool, "--code-only-data-backup", "--backup-receipt", str(receipt_path))
    uploads = [call for call in packet_tool.calls if call[0] == "scp"]
    assert [Path(call[-1].split(":", 1)[1]).name for call in uploads] == [
        "packet.tar.gz", "backup.json", "release.py"]
    assert not any("--owner-waiver" in " ".join(call) for call in packet_tool.calls)


@pytest.mark.parametrize(("options", "message"), [
    ((), "exactly one"),
    (("--owner-waiver", "W", "--backup-receipt", "R", "--code-only-data-backup"), "exactly one"),
    (("--owner-waiver", "W"), "code-only"),
    (("--owner-waiver", "W", "--code-only-data-backup", "--acknowledge-sanitized-url-loss"),
     "sanitized"),
    (("--owner-waiver", "W", "--code-only-data-backup", "--fallback-packet", "F"), "fallback"),
])
def test_deploy_packet_refuses_bad_option_combinations_before_ssh(packet_tool, options, message):
    path = packet_tool.tmp / "owner-waiver.json"
    release.write_json(path, waiver())
    receipt_path = packet_tool.tmp / "backup.json"
    release.write_json(receipt_path, receipt())
    mapping = {"W": str(path), "R": str(receipt_path), "F": str(packet_tool.packet)}
    # deploy_packet imports `release` top-level, so its error class is a distinct object.
    with pytest.raises(packet_tool.tool.ReleaseError, match=message):
        run_tool(packet_tool, *[mapping.get(option, option) for option in options])
    assert packet_tool.calls == []


@pytest.mark.parametrize("change", [
    {"revision": "d" * 40},
    {"given_at": (datetime.now(UTC) - timedelta(hours=30)).isoformat()},
    {"quote": ""},
])
def test_deploy_packet_checks_waiver_locally_before_touching_the_server(packet_tool, change):
    path = packet_tool.tmp / "owner-waiver.json"
    release.write_json(path, waiver(**change))
    with pytest.raises(packet_tool.tool.ReleaseError):
        run_tool(packet_tool, "--code-only-data-backup", "--owner-waiver", str(path))
    assert packet_tool.calls == []
