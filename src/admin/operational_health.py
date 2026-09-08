"""Measured refresh coverage, successful freshness, recovery and delivery backlog."""

from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from src.config import get_settings
from src.db.models import MatchDelivery, Platform, SiteListing, SubscriptionDelivery
from src.jobs.run_history import read_runs
from src.scrapers.freshness import RefreshCandidate, load_candidates, quota_estimates, utc


def _datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def schedules() -> dict[str, str]:
    result = {}
    for kind in ("nightly-refresh", "weekly-full-refresh", "scrape-smoke"):
        try:
            response = subprocess.run(
                [
                    "systemctl",
                    "show",
                    f"otz-{kind}.timer",
                    "--value",
                    "-p",
                    "NextElapseUSecRealtime",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            result[kind] = response.stdout.strip() if response.returncode == 0 else "unknown"
        except (OSError, subprocess.TimeoutExpired):
            result[kind] = "unknown"
    return result


def refresh_capacity(inventory: list[RefreshCandidate], *, settings, now: datetime) -> dict:
    """Estimate required capacity from inventory, never infer full run throughput.

    Listing state retains only its latest attempt. Legacy attempt timestamps do not
    establish an outcome; failures need the retry evidence written by record_attempt.
    """
    samples = {}
    for row in inventory:
        sample = samples.setdefault(row.platform, {"succeeded": 0, "failed": 0, "unknown": 0})
        if row.attempted_at is None or not timedelta(0) <= now - utc(row.attempted_at) <= timedelta(
            days=7
        ):
            continue
        attempt = utc(row.attempted_at)
        if row.succeeded_at is not None and attempt <= utc(row.succeeded_at) <= now:
            sample["succeeded"] += 1
        elif (
            row.failure_count > 0
            and row.next_retry_at is not None
            and utc(row.next_retry_at) >= attempt
        ):
            sample["failed"] += 1
        else:
            sample["unknown"] += 1
    rates = {
        platform: sample["succeeded"] / (sample["succeeded"] + sample["failed"])
        for platform, sample in samples.items()
        if sample["succeeded"] + sample["failed"]
    }
    estimates = quota_estimates(inventory, now=now, measured_success_rates=rates)
    for platform, sample in samples.items():
        estimates[platform.value]["latest_attempt_sample"] = sample
    minimum = sum(source["minimum_daily_successes"] for source in estimates.values())
    relevant = [source for source in estimates.values() if source["minimum_daily_successes"]]
    estimated_attempts = (
        sum(source["estimated_daily_attempts"] for source in relevant)
        if all(source["estimated_daily_attempts"] is not None for source in relevant)
        else None
    )
    quota = max(0, settings.nightly_detail_refresh)
    if quota < minimum:
        status = "insufficient_even_at_full_success"
    elif any(source["measured_success_rate"] == 0 for source in relevant):
        status = "no_observed_success"
    elif estimated_attempts is None:
        status = "unverified"
    elif quota < estimated_attempts:
        status = "insufficient_at_observed_rate"
    else:
        status = "estimate_only"
    return {
        "status": status,
        "sources": estimates,
        "active": sum(source["active"] for source in estimates.values()),
        "public_originals": sum(source["public"] for source in estimates.values()),
        "minimum_daily_successes": minimum,
        "configured_daily_attempts": quota,
        "configured_heal_attempts": max(0, settings.nightly_heal_limit),
        "shared_budget_sec": settings.nightly_refetch_budget_sec,
        "minimum_daily_shortfall": max(0, minimum - quota),
        "estimated_daily_attempts": estimated_attempts,
        "sample_window_days": 7,
        "sample_kind": "latest_attempt_per_listing",
        "attainment_confirmed": False,
    }


def proxy_service_receipt(path: Path, *, now: datetime) -> dict:
    """Read safe provider metadata; reject invalid, future or dated verification."""
    unknown = {"status": "unknown", "verified_at": None, "nearest_expiry": None, "orders": []}
    try:
        receipt = json.loads(path.read_text())
        if not isinstance(receipt, dict):
            return unknown
        verified = _datetime(receipt.get("verified_at"))
        orders = receipt.get("orders")
        if not verified or not isinstance(orders, list) or not 1 <= len(orders) <= 100:
            return unknown
        if verified > now + timedelta(minutes=5):
            return unknown
        safe_orders = []
        for order in orders:
            if not isinstance(order, dict):
                return unknown
            order_id = str(order.get("order_id", ""))
            expires = _datetime(order.get("expires_at"))
            subscription = order.get("subscription_enabled")
            if not order_id.isascii() or not order_id.isdigit() or len(order_id) > 20:
                return unknown
            if expires is None or not isinstance(subscription, bool):
                return unknown
            safe_orders.append(
                {"order_id": order_id, "expires_at": expires, "subscription_enabled": subscription}
            )
        nearest = min(order["expires_at"] for order in safe_orders)
        if now - verified > timedelta(hours=36):
            status = "dated"
        elif nearest <= now:
            status = "expired"
        elif nearest - now <= timedelta(days=3):
            status = "expiring"
        elif not all(order["subscription_enabled"] for order in safe_orders):
            status = "renewal_disabled"
        else:
            status = "verified"
        return {
            "status": status,
            "verified_at": verified,
            "nearest_expiry": nearest,
            "orders": safe_orders,
        }
    except (OSError, ValueError, TypeError):
        return unknown


async def build_operations(session, *, now: datetime | None = None) -> dict:
    now = utc(now or datetime.now(UTC))
    settings = get_settings()
    inventory = await load_candidates(session)
    capacity = refresh_capacity(inventory, settings=settings, now=now)
    runs = read_runs()
    sources = []
    reasons = []
    for platform in Platform:
        full_successes = [
            run
            for run in runs
            if run.get("kind") == "full"
            and run.get("finished_at")
            and any(
                source.get("platform") == platform.value and source.get("sweep_allowed")
                for source in run.get("sources", [])
            )
        ]
        last_full = max((_datetime(run["finished_at"]) for run in full_successes), default=None)
        stale_full = last_full is None or now - last_full > timedelta(days=8)
        source_capacity = capacity["sources"].get(platform.value, {})
        active = source_capacity.get("active", 0)
        fresh = source_capacity.get("fresh_active_7d", 0)
        latest = next(
            (
                source
                for run in runs
                for source in run.get("sources", [])
                if source.get("platform") == platform.value
            ),
            {},
        )
        ratio = fresh / active if active else 0.0
        if stale_full:
            reasons.append(f"{platform.value}: no verified full success in 8 days")
        if active and ratio < 0.95:
            reasons.append(f"{platform.value}: verified 7-day freshness below 95%")
        sources.append(
            {
                "platform": platform.value,
                "active": active,
                "fresh_7d": fresh,
                "fresh_pct": round(100 * ratio, 1),
                "last_full_success": last_full,
                "full_overdue": stale_full,
                "outcome": latest.get("outcome", "unknown"),
                "observed": latest.get("observed"),
                "total": latest.get("site_total"),
                "errors": latest.get("errors"),
                "blocked": latest.get("sweep_blocked_reasons", []),
                "proxy": latest.get("proxy_metrics", {}),
            }
        )
    public = capacity["public_originals"]
    public_fresh = sum(source["fresh_public_24h"] for source in capacity["sources"].values())
    public_overlays = await session.scalar(
        select(func.count(SiteListing.id)).where(SiteListing.is_public.is_(True))
    )
    if public_fresh < public:
        reasons.append("verified 24-hour public freshness below 100%")
    if capacity["status"].startswith("insufficient"):
        reasons.append("configured rolling refresh quota below daily capacity requirement")
    elif capacity["status"] == "no_observed_success":
        reasons.append("a source has no verified success in its latest-attempt sample")
    unresolved = {}
    for name, model in (("finder", MatchDelivery), ("subscriptions", SubscriptionDelivery)):
        unresolved[name] = dict(
            (
                await session.execute(
                    select(model.status, func.count())
                    .where(model.status != "sent")
                    .group_by(model.status)
                )
            ).all()
        )
    backup = {}
    receipt = settings.data_dir / "state" / "recovery_receipt.json"
    with suppress(OSError, ValueError):
        backup = json.loads(receipt.read_text())
    created = _datetime(backup.get("created_at"))
    backup_good = bool(
        created
        and now - created <= timedelta(hours=36)
        and backup.get("result") == "data_restore_verified"
        and backup.get("offhost_verified")
        and backup.get("protected_recovery_verified")
    )
    if not backup_good:
        reasons.append("verified off-host backup missing or older than 36 hours")
    proxy = proxy_service_receipt(
        settings.data_dir / "state" / "proxy_service_receipt.json", now=now
    )
    if proxy["status"] != "verified":
        reasons.append(f"proxy service verification: {proxy['status']}")
    for run in runs:
        if run.get("status") == "running":
            heartbeat = _datetime(run.get("heartbeat_at"))
            if heartbeat is None or now - heartbeat > timedelta(minutes=45):
                reasons.append("refresh has no recorded progress for more than 45 minutes")
    return {
        "status": "degraded" if reasons else "ok",
        "reasons": reasons,
        "sources": sources,
        "latest_run": runs[0] if runs else {},
        "schedules": await asyncio.to_thread(schedules),
        "public": public,
        "public_fresh": public_fresh,
        "public_overlays": public_overlays,
        "refresh_capacity": capacity,
        "deliveries": unresolved,
        "backup_created": created,
        "backup_verified": backup_good,
        "proxy_service": proxy,
        "proxy_expiry": (
            f"{proxy['status']}; {proxy['nearest_expiry'].isoformat()}"
            if proxy["nearest_expiry"]
            else "unknown; provider expiry not verified"
        ),
    }
