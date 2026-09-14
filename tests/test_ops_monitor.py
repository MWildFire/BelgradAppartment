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
        "proxy_service": {
            "status": "verified",
            "verified_at": NOW.isoformat(),
            "orders": [
                {
                    "order_id": "12345678",
                    "expires_at": (NOW + timedelta(days=17)).isoformat(),
                    "subscription_enabled": True,
                }
            ],
        },
        "refresh_capacity": {
            "status": "estimate_only",
            "minimum_daily_successes": 20,
            "configured_daily_attempts": 200,
            "estimated_daily_attempts": 40,
        },
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


def test_daily_target_cannot_reuse_weekly_success_or_alert_on_every_count_change():
    value = health()
    previous = evaluate_health(value, now=NOW)["next_state"]
    source = value["sources"][0]
    source.update(fresh_7d=100, fresh_for_target=94, fresh_target_hours=24, fresh_target_pct=100)
    event = evaluate_health(value, previous, now=NOW)
    assert event["emit"] and "nekretnine:active_freshness_below_target" in event["attention_added"]
    source["fresh_for_target"] = 95
    quiet = evaluate_health(value, event["next_state"], now=NOW)
    assert not quiet["emit"]
    source["fresh_for_target"] = 100
    recovered = evaluate_health(value, quiet["next_state"], now=NOW)
    assert recovered["emit"] and recovered["kind"] == "recovered"
    assert recovered["messages_sent"] == 0


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


def test_static_inadequate_quota_is_quiet_on_later_days_and_small_inventory_growth():
    value = health()
    value["status"] = "degraded"
    value["backup_verified"] = False
    value["refresh_capacity"].update(minimum_daily_successes=1948, estimated_daily_attempts=None)
    initial = evaluate_health(value, now=NOW)
    assert "refresh_capacity_insufficient_even_at_full_success" in initial["attention_added"]
    value["refresh_capacity"]["minimum_daily_successes"] = 1949
    value["proxy_service"]["verified_at"] = (NOW + timedelta(days=2)).isoformat()
    later = evaluate_health(value, initial["next_state"], now=NOW + timedelta(days=2))
    assert not later["emit"]
    assert later["next_state"]["signature"] == initial["next_state"]["signature"]


def test_capacity_thresholds_emit_even_when_other_conditions_remain_degraded():
    value = health()
    value["status"] = "degraded"
    value["backup_verified"] = False
    previous = evaluate_health(value, now=NOW)["next_state"]
    value["refresh_capacity"].update(minimum_daily_successes=1948, estimated_daily_attempts=None)
    insufficient = evaluate_health(value, previous, now=NOW)
    assert insufficient["emit"]
    value["refresh_capacity"]["configured_daily_attempts"] = 2000
    unknown = evaluate_health(value, insufficient["next_state"], now=NOW)
    assert unknown["emit"] and "refresh_capacity_unverified" in unknown["attention_added"]
    value["refresh_capacity"]["estimated_daily_attempts"] = 4000
    measured = evaluate_health(value, unknown["next_state"], now=NOW)
    assert "refresh_capacity_insufficient_at_observed_rate" in measured["attention_added"]
    value["refresh_capacity"]["configured_daily_attempts"] = 4000
    recovered = evaluate_health(value, measured["next_state"], now=NOW)
    assert recovered["emit"] and recovered["kind"] == "recovered"
    assert (
        "verified_backup_missing_or_stale" in recovered["next_state"]["snapshot"]["attention_codes"]
    )
    assert not evaluate_health(value, recovered["next_state"], now=NOW)["emit"]


def test_capacity_zero_success_and_invalid_counts_remain_unverified():
    value = health()
    previous = evaluate_health(value, now=NOW)["next_state"]
    value["refresh_capacity"].update(status="no_observed_success", estimated_daily_attempts=None)
    failure = evaluate_health(value, previous, now=NOW)
    assert "refresh_capacity_no_observed_success" in failure["attention_added"]
    value["refresh_capacity"]["configured_daily_attempts"] = "private invalid value"
    invalid = evaluate_health(value, failure["next_state"], now=NOW)
    assert "refresh_capacity_unverified" in invalid["attention_added"]
    assert "private" not in json.dumps(invalid)


def test_proxy_expiry_and_renewal_changes_emit_even_with_dated_receipt_and_degraded_health():
    value = health()
    value["status"] = "degraded"
    value["backup_verified"] = False
    value["proxy_service"]["verified_at"] = (NOW - timedelta(days=2)).isoformat()
    previous = evaluate_health(value, now=NOW)["next_state"]
    value["proxy_service"]["orders"][0]["subscription_enabled"] = False
    renewal = evaluate_health(value, previous, now=NOW)
    assert "proxy_renewal_disabled" in renewal["attention_added"]
    assert "proxy_verification_dated" in renewal["next_state"]["snapshot"]["attention_codes"]
    value["proxy_service"]["orders"][0]["expires_at"] = (NOW + timedelta(days=2)).isoformat()
    expiring = evaluate_health(value, renewal["next_state"], now=NOW)
    assert "proxy_expiring" in expiring["attention_added"]
    expired = evaluate_health(value, expiring["next_state"], now=NOW + timedelta(days=2, seconds=1))
    assert "proxy_expired" in expired["attention_added"]
    assert "proxy_expiring" in expired["attention_resolved"]
    assert not evaluate_health(value, expired["next_state"], now=NOW + timedelta(days=3))["emit"]


def test_proxy_receipt_crosses_36_hours_once_and_fresh_verification_recovers_once():
    value = health()
    previous = evaluate_health(value, now=NOW)["next_state"]
    dated = evaluate_health(value, previous, now=NOW + timedelta(hours=37))
    assert "proxy_verification_dated" in dated["attention_added"]
    assert not evaluate_health(value, dated["next_state"], now=NOW + timedelta(hours=38))["emit"]
    value["proxy_service"]["verified_at"] = (NOW + timedelta(hours=38)).isoformat()
    recovered = evaluate_health(value, dated["next_state"], now=NOW + timedelta(hours=38))
    assert recovered["emit"] and recovered["kind"] == "recovered"
    assert "proxy_verification_dated" in recovered["attention_resolved"]


def test_proxy_countdown_order_ids_and_regular_reverification_are_quiet_and_not_copied():
    value = health()
    previous = evaluate_health(value, now=NOW)["next_state"]
    value["proxy_service"]["verified_at"] = (NOW + timedelta(hours=12)).isoformat()
    value["proxy_service"]["orders"][0]["order_id"] = "private order identifier"
    value["proxy_service"]["orders"][0]["credentials"] = "private credential"
    value["proxy_service"]["endpoint"] = "private proxy endpoint"
    same = evaluate_health(value, previous, now=NOW + timedelta(hours=12))
    assert not same["emit"]
    assert "private" not in json.dumps(same)
    assert "expires_at" not in json.dumps(same)


def test_claimed_healthy_proxy_with_future_or_missing_evidence_is_unknown():
    value = health()
    previous = evaluate_health(value, now=NOW)["next_state"]
    value["proxy_service"]["verified_at"] = (NOW + timedelta(days=1)).isoformat()
    future = evaluate_health(value, previous, now=NOW)
    assert "proxy_verification_unknown" in future["attention_added"]
    value["proxy_service"]["orders"] = []
    assert not evaluate_health(value, future["next_state"], now=NOW)["emit"]
