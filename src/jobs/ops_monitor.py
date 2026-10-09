"""Read-only operational change detection for the owner heartbeat; no message transports.

Input is a private JSON snapshot from operational_health.build_operations. Output is a
sanitized structured event and next comparison state. The default CLI only reads files;
state persistence requires an explicit --write-state path supplied by the operator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TERMINAL_RUNS = frozenset({"success", "degraded", "failed", "cancelled", "skipped"})
SOURCE_OUTCOMES = frozenset({"ok", "success", "degraded", "failed", "unknown", "running"})
DELIVERY_FAILURES = frozenset({"failed", "ambiguous", "uncertain", "needs_reconciliation"})


def _date(value: Any) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _code(value: Any) -> str:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value)
        else "unknown"
    )


def _proxy_conditions(value: Any, now: datetime) -> tuple[dict[str, str], list[str]]:
    """Retain threshold states only; expiry countdown and order metadata do not cause noise."""
    unknown = {"verification": "unknown", "expiry": "unknown", "renewal": "unknown"}
    if not isinstance(value, Mapping):
        return unknown, ["proxy_verification_unknown"]
    verified = _date(value.get("verified_at"))
    orders = value.get("orders")
    if (
        verified is None
        or verified > now + timedelta(minutes=5)
        or not isinstance(orders, list)
        or not orders
    ):
        return unknown, ["proxy_verification_unknown"]
    expiries, subscriptions = [], []
    for order in orders:
        if not isinstance(order, Mapping):
            return unknown, ["proxy_verification_unknown"]
        expires, subscription = _date(order.get("expires_at")), order.get("subscription_enabled")
        if expires is None or not isinstance(subscription, bool):
            return unknown, ["proxy_verification_unknown"]
        expiries.append(expires)
        subscriptions.append(subscription)
    nearest = min(expiries)
    conditions = {
        "verification": "dated" if now - verified > timedelta(hours=36) else "current",
        "expiry": "expired"
        if nearest <= now
        else "expiring"
        if nearest - now <= timedelta(days=3)
        else "future",
        "renewal": "enabled" if all(subscriptions) else "disabled",
    }
    attention = []
    if conditions["verification"] == "dated":
        attention.append("proxy_verification_dated")
    if conditions["expiry"] in {"expired", "expiring"}:
        attention.append(f"proxy_{conditions['expiry']}")
    if conditions["renewal"] == "disabled":
        attention.append("proxy_renewal_disabled")
    return conditions, attention


def _capacity_condition(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "unverified"
    minimum, quota = value.get("minimum_daily_successes"), value.get("configured_daily_attempts")
    if any(
        not isinstance(number, int) or isinstance(number, bool) or number < 0
        for number in (minimum, quota)
    ):
        return "unverified"
    # Compare actual thresholds as well as the producer's label. Never infer measured
    # throughput from the quota or notify on ordinary count growth within one condition.
    if quota < minimum:
        return "insufficient_even_at_full_success"
    if value.get("status") == "no_observed_success":
        return "no_observed_success"
    estimated = value.get("estimated_daily_attempts")
    if not isinstance(estimated, int) or isinstance(estimated, bool) or estimated < minimum:
        return "unverified"
    return "insufficient_at_observed_rate" if quota < estimated else "estimate_only"


def evaluate_health(
    health: Mapping[str, Any],
    previous_state: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Emit only a changed actionable condition or newly completed run, never a repeated poll.

    Ordinary progress/count movement below a target does not repeatedly notify. Crossing
    the configured active or 100% public freshness target does. Failed delivery-count growth and
    resolution are meaningful; a merely pending queue is not called failed without age proof.
    Proxy verification/expiry/renewal and capacity conditions are independent of overall
    degraded status. Unchanged inadequate capacity is quiet even on a later calendar day.
    Raw reasons, summaries, URLs, recipients, source IDs and private payloads are not copied.
    """
    now = _date(now or datetime.now(UTC))
    if previous_state is not None and previous_state.get("version") != 1:
        raise ValueError("unsupported previous monitor state")
    previous = dict((previous_state or {}).get("snapshot", {}))
    attention = []
    sources = {}
    for entry in health.get("sources", []):
        name = _code(entry.get("platform"))
        active = _count(entry.get("active"))
        if "fresh_for_target" in entry:
            fresh = _count(entry.get("fresh_for_target"))
            target_pct = entry.get("fresh_target_pct")
            target = target_pct / 100 if type(target_pct) is int and target_pct in {95, 100} else 1
        else:
            fresh, target = _count(entry.get("fresh_7d")), 0.95
        fresh_enough = active == 0 or fresh / active >= target
        overdue = bool(entry.get("full_overdue", True))
        outcome = entry.get("outcome", "unknown")
        outcome = outcome if outcome in SOURCE_OUTCOMES else "unknown"
        sources[name] = {
            "freshness_target_met": fresh_enough,
            "full_overdue": overdue,
            "outcome": outcome,
        }
        if not fresh_enough:
            attention.append(f"{name}:active_freshness_below_target")
        if overdue:
            attention.append(f"{name}:verified_full_overdue")
        if outcome in {"failed", "degraded", "unknown"}:
            attention.append(f"{name}:source_{outcome}")
    if not sources:
        attention.append("source_evidence_missing")
    public, public_fresh = _count(health.get("public")), _count(health.get("public_fresh"))
    public_good = public_fresh >= public
    if not public_good:
        attention.append("public_freshness_below_target")
    backup_good = health.get("backup_verified") is True
    if not backup_good:
        attention.append("verified_backup_missing_or_stale")
    proxy, proxy_attention = _proxy_conditions(health.get("proxy_service"), now)
    attention.extend(proxy_attention)
    capacity = _capacity_condition(health.get("refresh_capacity"))
    if capacity != "estimate_only":
        attention.append(f"refresh_capacity_{capacity}")
    delivery_failures = {}
    for kind in ("finder", "subscriptions"):
        counts = health.get("deliveries", {}).get(kind, {})
        delivery_failures[kind] = sum(_count(counts.get(status)) for status in DELIVERY_FAILURES)
        if delivery_failures[kind]:
            attention.append(f"{kind}:delivery_failure_requires_review")
    latest = health.get("latest_run", {})
    latest_status = latest.get("status")
    finished_at = _date(latest.get("finished_at"))
    completed_id = previous.get("completed_run_id")
    completed_status = previous.get("completed_run_status")
    if latest_status in TERMINAL_RUNS and finished_at and finished_at <= now:
        completed_id, completed_status = _code(latest.get("run_id")), latest_status
    if completed_status in {"failed", "degraded", "cancelled"}:
        attention.append(f"refresh_run_{completed_status}")
    if latest_status == "running":
        heartbeat = _date(latest.get("heartbeat_at"))
        if heartbeat is None or now - heartbeat > timedelta(minutes=45):
            attention.append("refresh_progress_overdue")
        elif heartbeat > now + timedelta(minutes=5):
            attention.append("refresh_clock_evidence_invalid")
    # Public-only checkpoints never replace the main full/incremental run identity.
    public_completed_id = previous.get("completed_public_refresh_id")
    public_completed_status = previous.get("completed_public_refresh_status")
    public_refresh = health.get("public_refresh")
    if isinstance(public_refresh, Mapping):
        public_status = public_refresh.get("status")
        public_at = _date(public_refresh.get("checkpoint_at"))
        public_id = _code(public_refresh.get("run_id"))
        if public_status not in TERMINAL_RUNS | {"running", "missing", "unknown", "stalled"}:
            attention.append("public_refresh_unknown")
        if (public_refresh.get("final") is True and public_status in TERMINAL_RUNS
                and public_id != "unknown"):
            if public_at and public_at <= now:
                public_completed_id = public_id
                public_completed_status = public_status
            else:
                attention.append("public_refresh_unknown")
        if public_status in {"missing", "unknown", "stalled"}:
            attention.append(f"public_refresh_{public_status}")
        if public_status == "running" and any(_count(public_refresh.get(name)) for name in (
            "errors", "skipped", "source_error_count",
        )):
            attention.append("public_refresh_errors")
        if public_refresh.get("overdue") is True:
            attention.append("public_refresh_overdue")
    elif "public_refresh" in health:
        attention.append("public_refresh_unknown")
    if public_completed_status in {"failed", "degraded", "cancelled"}:
        attention.append(f"public_refresh_{public_completed_status}")
    reported_status = health.get("status")
    if reported_status not in {"ok", "degraded"}:
        attention.append("operational_health_unavailable")
    elif reported_status == "degraded" and not attention:
        attention.append("operational_health_degraded")
    snapshot = {
        "sources": sources,
        "public_freshness_target_met": public_good,
        "backup_verified": backup_good,
        "proxy_service": proxy,
        "refresh_capacity": capacity,
        "delivery_failures": delivery_failures,
        "completed_run_id": completed_id,
        "completed_run_status": completed_status,
        "completed_public_refresh_id": public_completed_id,
        "completed_public_refresh_status": public_completed_status,
        "attention_codes": sorted(set(attention)),
    }
    signature = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    prior_attention = set(previous.get("attention_codes", []))
    added = sorted(set(attention) - prior_attention)
    resolved = sorted(prior_attention - set(attention))
    new_completion = bool(completed_id and completed_id != previous.get("completed_run_id"))
    new_public_completion = bool(
        public_completed_id
        and (public_completed_id != previous.get("completed_public_refresh_id")
             or public_completed_status != previous.get("completed_public_refresh_status"))
    )
    changed_deliveries = bool(previous) and delivery_failures != previous.get("delivery_failures")
    targets_changed = bool(previous) and (
        public_good != previous.get("public_freshness_target_met")
        or sources != previous.get("sources")
        or backup_good != previous.get("backup_verified")
        or ("proxy_service" in previous and proxy != previous["proxy_service"])
        or ("refresh_capacity" in previous and capacity != previous["refresh_capacity"])
    )
    emit = bool(added or resolved or new_completion or new_public_completion
                or changed_deliveries or targets_changed)
    event_kind = (
        "attention"
        if added or (any(delivery_failures.values()) and changed_deliveries)
        else (
            "run_completed"
            if new_completion or new_public_completion
            else "recovered"
            if resolved
            else "changed"
            if emit
            else "quiet"
        )
    )
    return {
        "emit": emit,
        "kind": event_kind,
        "attention_added": added,
        "attention_resolved": resolved,
        "run_completed": new_completion or new_public_completion,
        "completed_run_status": completed_status if new_completion else None,
        "public_refresh_completed": new_public_completion,
        "completed_public_refresh_status": (
            public_completed_status if new_public_completion else None
        ),
        "delivery_failures": delivery_failures,
        "read_only": True,
        "messages_sent": 0,
        "next_state": {
            "version": 1,
            "observed_at": now.isoformat(),
            "signature": signature,
            "snapshot": snapshot,
        },
    }


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    """Explicit private local persistence, separate from event evaluation and external sends."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("monitor state parent must be private (0700)")
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(state, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-file", type=Path, required=True)
    parser.add_argument("--previous-state", type=Path)
    parser.add_argument(
        "--write-state",
        type=Path,
        help="explicitly persist next state privately; default only reads",
    )
    args = parser.parse_args()
    health = json.loads(args.health_file.read_text())
    previous = json.loads(args.previous_state.read_text()) if args.previous_state else None
    event = evaluate_health(health, previous)
    if args.write_state:
        save_state(args.write_state, event["next_state"])
        event["read_only"] = False
    print(json.dumps(event, sort_keys=True))


if __name__ == "__main__":
    main()
