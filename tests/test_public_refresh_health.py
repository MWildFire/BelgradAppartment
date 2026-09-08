"""Public maintenance outcomes must survive alongside the full-crawl history."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.admin import operational_health as health
from src.config import get_settings
from src.db import get_session
from src.jobs.ops_monitor import evaluate_health

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


def checkpoint(**changes):
    value = {
        "run_id": "a" * 32, "checkpoint_at": NOW.isoformat(), "final": True,
        "stop_reason": "completed", "selected": 3, "updated": 3, "verified": 3,
        "photos": 3, "errors": 0, "deactivated": 0, "skipped": 0,
        "source_errors": {}, "price_changes": 0,
    }
    return value | changes


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(health, "schedules", lambda: {})
    monkeypatch.setattr(health, "read_runs", lambda: [])
    path = tmp_path / "state/freshness/public_refetch.json"
    path.parent.mkdir(parents=True)
    return path


async def report():
    async with get_session() as session:
        return await health.build_operations(session, now=NOW)


async def test_public_failure_visible_immediately_and_does_not_claim_full_success(db, isolated):
    isolated.write_text(json.dumps(checkpoint(
        errors=1, verified=2,
        source_errors={"NEKRETNINE": "TimeoutError"},
        private_payload="contact and private URL must not escape",
    )))
    result = await report()
    public = result["public_refresh"]
    assert public["status"] == "degraded"
    assert public["errors"] == 1 and public["verified"] == 2
    assert public["source_errors"] == {"NEKRETNINE": "TimeoutError"}
    assert any("public detail refresh" in reason for reason in result["reasons"])
    assert result["latest_run"] == {}
    assert all(source["last_full_success"] is None for source in result["sources"])
    assert "private" not in json.dumps(public, default=str)
    from pathlib import Path
    from types import SimpleNamespace

    from jinja2 import Environment, FileSystemLoader

    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "src/admin/templates"), autoescape=True
    )
    rendered = environment.get_template("health.html").render(
        report={"now": NOW, "operations": result, "platforms": [],
                "monitor": {"status": "warn", "last_inbound_at": None},
                "fingerprint": {"present": False}},
        request=SimpleNamespace(url=SimpleNamespace(path="/health"),
                                state=SimpleNamespace(csrf_token="fixture")),
    )
    assert "ошибками</strong>" in rendered and "подтверждено 2" in rendered
    assert "NEKRETNINE: TimeoutError" in rendered
    assert "contact and private URL" not in rendered


@pytest.mark.parametrize("change,status", [
    ({"stop_reason": "cancelled"}, "cancelled"),
    ({"stop_reason": "failed"}, "failed"),
    ({"stop_reason": "budget_exhausted", "skipped": 1}, "degraded"),
    ({"stop_reason": "degraded", "source_errors": {"HALOOGLASI": "TimeoutError"}}, "degraded"),
])
async def test_terminal_public_outcomes_are_not_turned_into_success(db, isolated, change, status):
    isolated.write_text(json.dumps(checkpoint(**change)))
    assert (await report())["public_refresh"]["status"] == status


async def test_public_progress_and_main_refresh_checkpoint_remain_independent(db, isolated):
    isolated.write_text(json.dumps(checkpoint(final=False, stop_reason="running", verified=1)))
    isolated.with_name("refetch.json").write_text(json.dumps(checkpoint(run_id="b" * 32)))
    public = (await report())["public_refresh"]
    assert public["status"] == "running" and public["run_id"] == "a" * 32
    assert public["verified"] == 1 and public["final"] is False


async def test_old_public_completion_exposes_missed_cadence(db, isolated):
    isolated.write_text(json.dumps(checkpoint(
        checkpoint_at=(NOW - timedelta(hours=13, seconds=1)).isoformat(),
    )))
    result = await report()
    assert result["public_refresh"]["status"] == "success"
    assert result["public_refresh"]["overdue"] is True
    assert "public detail refresh has no checkpoint in 13 hours" in result["reasons"]


@pytest.mark.parametrize("value,status", [
    (None, "missing"),
    ([], "unknown"),
    (checkpoint(checkpoint_at=(NOW + timedelta(hours=1)).isoformat()), "unknown"),
    (checkpoint(errors="private error"), "unknown"),
    (checkpoint(final=False, stop_reason="running",
                checkpoint_at=(NOW - timedelta(minutes=46)).isoformat()), "stalled"),
])
async def test_missing_invalid_future_or_stalled_evidence_is_explicit(db, isolated, value, status):
    if value is not None:
        isolated.write_text(json.dumps(value))
    public = (await report())["public_refresh"]
    assert public["status"] == status
    assert "private" not in json.dumps(public, default=str)


def test_monitor_public_completion_and_failure_are_independent_and_quiet_when_unchanged():
    from tests.test_ops_monitor import health as healthy_snapshot

    value = healthy_snapshot()
    previous = evaluate_health(value, now=NOW)["next_state"]
    value["public_refresh"] = checkpoint(status="degraded", errors=1, verified=2,
                                        source_error_count=0, overdue=False)
    event = evaluate_health(value, previous, now=NOW)
    assert event["emit"] and event["public_refresh_completed"]
    assert event["completed_public_refresh_status"] == "degraded"
    assert "public_refresh_degraded" in event["attention_added"]
    assert event["completed_run_status"] is None
    quiet = evaluate_health(value, event["next_state"], now=NOW)
    assert not quiet["emit"]
    value["latest_run"] = {"run_id": "main-run", "status": "success",
                           "finished_at": NOW.isoformat()}
    main_completed = evaluate_health(value, quiet["next_state"], now=NOW)
    assert not main_completed["public_refresh_completed"]
    assert main_completed["next_state"]["snapshot"]["completed_public_refresh_id"] == "a" * 32
    assert "public_refresh_degraded" in main_completed["next_state"]["snapshot"]["attention_codes"]
    value["public_refresh"]["run_id"] = "b" * 32
    another = evaluate_health(value, main_completed["next_state"], now=NOW)
    assert another["emit"] and another["public_refresh_completed"]
    value["public_refresh"] = checkpoint(run_id="c" * 32, status="success",
                                        source_error_count=0, overdue=False)
    recovered = evaluate_health(value, another["next_state"], now=NOW)
    assert recovered["emit"] and "public_refresh_degraded" in recovered["attention_resolved"]
    assert recovered["messages_sent"] == 0
    assert not evaluate_health(value, recovered["next_state"], now=NOW)["emit"]


def test_monitor_running_public_progress_is_quiet_but_first_error_is_actionable():
    from tests.test_ops_monitor import health as healthy_snapshot

    value = healthy_snapshot()
    value["public_refresh"] = checkpoint(status="running", final=False, verified=0,
                                        source_error_count=0, overdue=False)
    previous = evaluate_health(value, now=NOW)["next_state"]
    value["public_refresh"]["verified"] = 1
    assert not evaluate_health(value, previous, now=NOW)["emit"]
    value["public_refresh"]["errors"] = 1
    event = evaluate_health(value, previous, now=NOW)
    assert event["emit"] and "public_refresh_errors" in event["attention_added"]
    assert not event["public_refresh_completed"]
    assert not evaluate_health(value, event["next_state"], now=NOW)["emit"]
