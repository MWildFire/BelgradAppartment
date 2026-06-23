"""C2 — nightly saved-search matcher: new public listings × active subscriptions → digests.

For each ACTIVE subscription, find public listings that (a) match its criteria via the SHARED
`listing_matches_filters` predicate (the same one the catalog uses), (b) are NEW — not yet in the
subscription_matches ledger, and (c) were exposed within `since_hours`. The matches are grouped
into ONE digest per subscription and sent over its channel (e-mail or Telegram). A
subscription_matches row is written per (subscription, listing) only AFTER a successful send, so:

- Idempotent: a re-run sends nothing new (the UNIQUE(subscription_id, site_listing_id) ledger
  already records every delivered listing).
- Fault-isolated: one subscription's failed send doesn't abort the batch and doesn't record a
  ledger row, so that subscription is retried next run.

This is a write path on the catalog DB, so it uses the site writer engine (busy_timeout) for the
ledger insert, and the read-only engine for candidate enumeration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.config import get_settings
from src.db.models import Subscription, SubscriptionMatch
from src.site import db as sitedb
from src.site.email import send_subscription_digest
from src.site.readmodel import (
    Filters,
    SubscriptionCandidate,
    listing_matches_filters,
    public_candidates,
)
from src.telegram.subscription_delivery import send_subscription_digest_telegram

logger = logging.getLogger(__name__)


@dataclass
class NotifyStats:
    """Outcome of one notify_subscriptions run."""

    subscriptions_considered: int = 0
    subscriptions_notified: int = 0
    listings_sent: int = 0
    errors: int = 0


def _filters_from_criteria(criteria: dict) -> Filters:
    """Rebuild a readmodel.Filters from stored criteria (mirror of the Filters fields).

    show_rented stays False: a subscription must NEVER notify about a rented apartment.
    """
    return Filters(
        vnj=bool(criteria.get("vnj")),
        wc=bool(criteria.get("wc")),
        district=list(criteria.get("district") or []),
        price_min=criteria.get("price_min"),
        price_max=criteria.get("price_max"),
        rooms=list(criteria.get("rooms") or []),
        show_rented=False,
    )


def _unsubscribe_url(token: str) -> str:
    base = (get_settings().site_public_base_url or "").rstrip("/")
    return f"{base}/unsubscribe?token={token}"


async def _active_subscriptions(database_url: str | None) -> list[Subscription]:
    async with sitedb.read_session(database_url=database_url) as s:
        return list(
            (
                await s.execute(
                    select(Subscription)
                    .where(Subscription.is_active.is_(True))
                    .order_by(Subscription.id)
                )
            ).scalars()
        )


async def _already_notified(
    database_url: str | None, subscription_id: int
) -> set[int]:
    async with sitedb.read_session(database_url=database_url) as s:
        rows = (
            await s.execute(
                select(SubscriptionMatch.site_listing_id).where(
                    SubscriptionMatch.subscription_id == subscription_id
                )
            )
        ).all()
        return {r[0] for r in rows}


def _new_matches(
    sub: Subscription,
    candidates: list[SubscriptionCandidate],
    already: set[int],
    cutoff: datetime,
) -> list[SubscriptionCandidate]:
    """Candidates that match the criteria, are fresh (created >= cutoff), and not yet notified."""
    f = _filters_from_criteria(sub.criteria or {})
    out: list[SubscriptionCandidate] = []
    for c in candidates:
        if c.view.id in already:
            continue
        created = c.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < cutoff:
            continue
        if listing_matches_filters(c.view, c.rooms_raw, f):
            out.append(c)
    return out


def _deliverable(sub: Subscription) -> bool:
    """Can we actually deliver to this subscription right now?

    - email: needs an e-mail address.
    - telegram: needs a linked chat_id (an unlinked telegram sub has none → skip; it stays in the
      ledger-free state and gets nothing until the user links via /start).
    """
    if sub.channel == "email":
        return bool(sub.email)
    if sub.channel == "telegram":
        return sub.telegram_chat_id is not None
    return False


async def _send_digest(sub: Subscription, listings: list, unsubscribe_url: str) -> None:
    """Dispatch the digest over the subscription's channel. Raises on send failure."""
    if sub.channel == "email":
        await send_subscription_digest(sub.email, listings, unsubscribe_url)
    elif sub.channel == "telegram":
        await send_subscription_digest_telegram(sub.telegram_chat_id, listings, unsubscribe_url)
    else:  # pragma: no cover - guarded by _deliverable
        raise ValueError(f"unknown channel {sub.channel!r}")


# A single digest is capped at this many listings — the Telegram message limit is 4096 chars,
# and a fresh subscription shouldn't blast the whole back-catalog. Overflow is simply not
# recorded, so the next run picks it up: a 100-match subscription drains 30/night.
SUBSCRIPTION_DIGEST_MAX = 30


async def _record(
    database_url: str | None, subscription_id: int, site_listing_ids: list[int]
) -> None:
    """Write subscription_matches rows AFTER a successful send (idempotency ledger).

    Idempotent: INSERT … ON CONFLICT DO NOTHING on the UNIQUE(subscription_id, site_listing_id)
    guard, so a concurrent run or a partial retry can never raise an IntegrityError that would
    escape and abort the whole batch. `notified_at` is set explicitly because the raw insert
    bypasses the ORM column default.
    """
    if not site_listing_ids:
        return
    now = datetime.now(timezone.utc)
    async with sitedb.writer_session(database_url=database_url) as s:
        await s.execute(
            sqlite_insert(SubscriptionMatch)
            .values(
                [
                    {
                        "subscription_id": subscription_id,
                        "site_listing_id": sl_id,
                        "notified_at": now,
                    }
                    for sl_id in site_listing_ids
                ]
            )
            .on_conflict_do_nothing(index_elements=["subscription_id", "site_listing_id"])
        )


async def notify_subscriptions(
    *, since_hours: int = 24, dry_run: bool = False, database_url: str | None = None
) -> NotifyStats:
    """Build + send one saved-search digest per active subscription, idempotently.

    Returns counts. Per-subscription fault isolation: one bad send increments `errors` and
    leaves that subscription's ledger untouched (retried next run) without aborting the batch.
    `dry_run` computes + reports what WOULD be sent but neither sends nor records.
    """
    stats = NotifyStats()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    subs = await _active_subscriptions(database_url)
    if not subs:
        return stats

    candidates = await public_candidates(database_url=database_url)

    for sub in subs:
        stats.subscriptions_considered += 1
        if not _deliverable(sub):
            logger.info(
                "[notify-subs] sub=%d not deliverable (channel=%s) — skip", sub.id, sub.channel
            )
            continue
        already = await _already_notified(database_url, sub.id)
        new = _new_matches(sub, candidates, already, cutoff)
        if not new:
            continue

        # Cap the digest; overflow is left unrecorded so it flows into the next run.
        batch = new[:SUBSCRIPTION_DIGEST_MAX]
        views = [c.view for c in batch]
        if dry_run:
            stats.subscriptions_notified += 1
            stats.listings_sent += len(views)
            continue

        try:
            await _send_digest(sub, views, _unsubscribe_url(sub.token))
        except Exception:
            # One subscription's send must never abort the batch. No ledger row → retried next run.
            logger.exception("[notify-subs] sub=%d digest send failed", sub.id)
            stats.errors += 1
            continue

        try:
            # Record ONLY what we sent. Idempotent + inside the try: a ledger failure after a
            # successful send is isolated to this subscription (worst case a re-send next run)
            # instead of escaping uncaught and aborting every remaining subscription.
            await _record(database_url, sub.id, [c.view.id for c in batch])
        except Exception:
            logger.exception("[notify-subs] sub=%d ledger record failed post-send", sub.id)
            stats.errors += 1
            continue

        stats.subscriptions_notified += 1
        stats.listings_sent += len(views)

    logger.info(
        "[notify-subs] considered=%d notified=%d listings=%d errors=%d",
        stats.subscriptions_considered, stats.subscriptions_notified,
        stats.listings_sent, stats.errors,
    )
    return stats
