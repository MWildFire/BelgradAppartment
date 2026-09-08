"""Only meaningful sanitized operational changes should wake the owner heartbeat."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.jobs.ops_monitor import evaluate_health, save_state

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


def health():
    return {
        "status": "ok",
        "sources": [
            {
                "platform": "nekretnine",
                "active": 100,
                "fresh_7d": 97,
                "full_overdue": False,
                "outcome": "ok",
            }
        ],
        "public": 10,
        "public_fresh": 10,
        "backup_verified": True,
        "deliveries": {"finder": {"pending": 50}, "subscriptions": {"sent": 20}},
        "latest_run": {},
        "raw_secret": "never copy this private payload",
        "reasons": ["private unstructured diagnostic"],
    }


def test_unchanged_and_minor_count_changes_are_quiet():
    value = health()
    first = evaluate_health(value, now=NOW)
    assert not first["emit"]
    value["sources"][0]["fresh_7d"] = 98
    value["deliveries"]["finder"]["pending"] = 60
    second = evaluate_health(value, first["next_state"], now=NOW + timedelta(minutes=5))
    assert not second["emit"] and second["messages_sent"] == 0
    assert first["next_state"]["signature"] == second["next_state"]["signature"]
    assert "private" not in json.dumps(second)


def test_backup_expiry_alert_once_and_recovery_once():
    value = health()
    previous = evaluate_health(value, now=NOW)["next_state"]
    value["backup_verified"] = False
    event = evaluate_health(value, previous, now=NOW)
    assert event["emit"] and event["kind"] == "attention"
    assert "verified_backup_missing_or_stale" in event["attention_added"]
    quiet = evaluate_health(value, event["next_state"], now=NOW)
    assert not quiet["emit"]
    value["backup_verified"] = True
    recovered = evaluate_health(value, quiet["next_state"], now=NOW)
    assert recovered["emit"] and recovered["kind"] == "recovered"


def test_new_automatic_run_completion_notifies_even_when_still_degraded():
    value = health()
    value["status"] = "degraded"
    previous = evaluate_health(value, now=NOW)["next_state"]
    value["latest_run"] = {
        "run_id": "run123",
        "status": "degraded",
        "finished_at": NOW.isoformat(),
        "summary": ["private details"],
    }
    event = evaluate_health(value, previous, now=NOW)
    assert event["emit"] and event["run_completed"]
    assert not evaluate_health(value, event["next_state"], now=NOW)["emit"]
    assert "private" not in json.dumps(event)


def test_running_transition_quiet_then_stalled_becomes_actionable():
    value = health()
    previous = evaluate_health(value, now=NOW)["next_state"]
    value["latest_run"] = {"run_id": "run123", "status": "running", "heartbeat_at": NOW.isoformat()}
    running = evaluate_health(value, previous, now=NOW)
    assert not running["emit"]
    stale = evaluate_health(value, running["next_state"], now=NOW + timedelta(minutes=46))
    assert stale["emit"] and "refresh_progress_overdue" in stale["attention_added"]


def test_freshness_threshold_crossing_and_delivery_failure_are_meaningful():
    value = health()
    previous = evaluate_health(value, now=NOW)["next_state"]
    value["sources"][0]["fresh_7d"] = 94
    value["public_fresh"] = 9
    value["deliveries"]["finder"]["ambiguous"] = 1
    event = evaluate_health(value, previous, now=NOW)
    assert event["emit"] and len(event["attention_added"]) == 3
    value["sources"][0]["fresh_7d"] = 93
    assert not evaluate_health(value, event["next_state"], now=NOW)["emit"]


def test_default_cli_reads_without_writing_or_messages(tmp_path):
    value = health()
    source = tmp_path / "health.json"
    source.write_text(json.dumps(value))
    before = source.read_bytes()
    result = subprocess.run(
        [sys.executable, "-m", "src.jobs.ops_monitor", "--health-file", str(source)],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    event = json.loads(result.stdout)
    assert event["read_only"] and event["messages_sent"] == 0
    assert source.read_bytes() == before and list(tmp_path.iterdir()) == [source]


def test_explicit_saved_state_private_and_atomic(tmp_path):
    path = tmp_path / "private" / "state.json"
    event = evaluate_health(health(), now=NOW)
    save_state(path, event["next_state"])
    assert json.loads(path.read_text()) == event["next_state"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not list(path.parent.glob("*.tmp"))
