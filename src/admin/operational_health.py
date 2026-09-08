"""Measured refresh coverage, successful freshness, recovery and delivery backlog."""

from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from src.config import get_settings
from src.db.models import Listing, Match, MatchDelivery, Platform, SiteListing, SubscriptionDelivery
from src.jobs.run_history import read_runs


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


async def build_operations(session, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
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
        active, fresh = (
            await session.execute(
                select(
                    func.count(Listing.id),
                    func.count(Listing.id).filter(
                        Listing.details_succeeded_at >= now - timedelta(days=7)
                    ),
                ).where(Listing.platform == platform, Listing.is_active.is_(True))
            )
        ).one()
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
    public, public_fresh = (
        await session.execute(
            select(
                func.count(func.distinct(Listing.id)),
                func.count(func.distinct(Listing.id)).filter(
                    Listing.details_succeeded_at >= now - timedelta(hours=24)
                ),
            )
            .join(Match, Match.listing_id == Listing.id)
            .join(SiteListing, SiteListing.match_id == Match.id)
            .where(SiteListing.is_public.is_(True))
        )
    ).one()
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
    receipt = get_settings().data_dir / "state" / "recovery_receipt.json"
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
        "deliveries": unresolved,
        "backup_created": created,
        "backup_verified": backup_good,
        "proxy_expiry": "unknown; provider expiry not verified",
    }
