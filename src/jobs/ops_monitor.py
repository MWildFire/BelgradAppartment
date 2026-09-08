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


def evaluate_health(
    health: Mapping[str, Any],
    previous_state: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Emit only a changed actionable condition or newly completed run, never a repeated poll.

    Ordinary progress/count movement below a target does not repeatedly notify. Crossing
    the 95% active or 100% public freshness target does. Failed delivery-count growth and
    resolution are meaningful; a merely pending queue is not called failed without age proof.
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
        active, fresh = _count(entry.get("active")), _count(entry.get("fresh_7d"))
        fresh_enough = active == 0 or fresh / active >= 0.95
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
    reported_status = health.get("status")
    if reported_status not in {"ok", "degraded"}:
        attention.append("operational_health_unavailable")
    elif reported_status == "degraded" and not attention:
        attention.append("operational_health_degraded")
    snapshot = {
        "sources": sources,
        "public_freshness_target_met": public_good,
        "backup_verified": backup_good,
        "delivery_failures": delivery_failures,
        "completed_run_id": completed_id,
        "completed_run_status": completed_status,
        "attention_codes": sorted(set(attention)),
    }
    signature = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    prior_attention = set(previous.get("attention_codes", []))
    added = sorted(set(attention) - prior_attention)
    resolved = sorted(prior_attention - set(attention))
    new_completion = bool(completed_id and completed_id != previous.get("completed_run_id"))
    changed_deliveries = bool(previous) and delivery_failures != previous.get("delivery_failures")
    targets_changed = bool(previous) and (
        public_good != previous.get("public_freshness_target_met")
        or sources != previous.get("sources")
        or backup_good != previous.get("backup_verified")
    )
    emit = bool(added or resolved or new_completion or changed_deliveries or targets_changed)
    event_kind = (
        "attention"
        if added or (any(delivery_failures.values()) and changed_deliveries)
        else (
            "run_completed"
            if new_completion
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
        "run_completed": new_completion,
        "completed_run_status": completed_status if new_completion else None,
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
