"""Saved-search discovery feeds a durable queue before bounded, confirmed digest delivery.

The initial window selects recent public cards. A persisted cursor then covers scheduler gaps;
queued items survive age windows and transport outages. Atomic claims prevent parallel duplicate
sends, while transport ambiguity requires reconciliation. A confirmed ledger is written only
in the same transaction as the corresponding sent outcome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.config import get_settings
from src.db.models import Subscription, SubscriptionDelivery, SubscriptionMatch
from src.jobs import subscription_outbox as outbox
from src.site import db as sitedb
from src.site.email import send_subscription_digest
from src.site.readmodel import (
    Filters,
    SubscriptionCandidate,
    listing_matches_filters,
    public_candidates,
)
from src.telegram.delivery_ledger import ambiguous_send_error
from src.telegram.subscription_delivery import send_subscription_digest_telegram

logger = logging.getLogger(__name__)


@dataclass
class NotifyStats:
    """Outcome of one notify_subscriptions run."""

    subscriptions_considered: int = 0
    subscriptions_notified: int = 0
    listings_sent: int = 0
    errors: int = 0
    pending: int = 0
    unknown: int = 0


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
            created = created.replace(tzinfo=UTC)
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


async def _send_digest(sub: Subscription, listings: list, unsubscribe_url: str) -> bool:
    """Dispatch the digest over the subscription's channel. Raises on send failure."""
    if sub.channel == "email":
        return await send_subscription_digest(sub.email, listings, unsubscribe_url)
    elif sub.channel == "telegram":
        return await send_subscription_digest_telegram(
            sub.telegram_chat_id, listings, unsubscribe_url
        )
    else:  # pragma: no cover - guarded by _deliverable
        raise ValueError(f"unknown channel {sub.channel!r}")


# A single digest is capped at this many listings — the Telegram message limit is 4096 chars,
# and a fresh subscription should not blast the whole back-catalog. The durable queue keeps
# every overflow item until a later confirmed send, independent of its original age.
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
    now = datetime.now(UTC)
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
    now = datetime.now(UTC)
    initial_cutoff = now - timedelta(hours=since_hours)
    subs = await _active_subscriptions(database_url)
    if not subs:
        return stats
    candidates = await public_candidates(database_url=database_url)

    for sub in subs:
        stats.subscriptions_considered += 1
        already = await _already_notified(database_url, sub.id)
        cutoff = sub.notification_cursor_at or initial_cutoff
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        new = _new_matches(sub, candidates, already, cutoff)
        if not dry_run:
            # Capture the entire discovered set BEFORE applying digest limits or attempting
            # delivery. A 72-hour transport outage cannot age queued work out of existence.
            await outbox.enqueue(database_url, sub.id, [c.view.id for c in new], now)
        pending = await outbox.pending_ids(database_url, sub.id)
        if dry_run:
            pending = list(dict.fromkeys([*pending, *(c.view.id for c in new)]))
        if not _deliverable(sub):
            continue
        f = _filters_from_criteria(sub.criteria or {})
        available = {c.view.id: c for c in candidates
                     if listing_matches_filters(c.view, c.rooms_raw, f)}
        # Re-check public authorization/availability now; unpublish/rented does not leak from
        # an earlier queue capture. Such work remains queued for a possible later recovery.
        ids = [sl_id for sl_id in pending if sl_id in available and sl_id not in already]
        ids = ids[:SUBSCRIPTION_DIGEST_MAX]
        if not ids:
            continue
        if dry_run:
            stats.subscriptions_notified += 1
            stats.listings_sent += len(ids)
            continue
        token, claimed = await outbox.claim(database_url, sub.id, ids)
        if not claimed:
            continue
        views = [available[sl_id].view for sl_id in ids if sl_id in claimed]
        try:
            confirmed = await _send_digest(sub, views, _unsubscribe_url(sub.token))
        except Exception as error:
            await outbox.finish(database_url, sub.id, token,
                                status="unknown" if ambiguous_send_error(error) else "failed",
                                error_code=type(error).__name__[:64])
            logger.warning("[notify-subs] sub=%d send=%s", sub.id, type(error).__name__)
            stats.errors += 1
            continue
        if confirmed is not True:
            await outbox.finish(database_url, sub.id, token, status="failed",
                                error_code="transport_not_confirmed")
            stats.errors += 1
            continue
        try:
            await outbox.finish(database_url, sub.id, token, status="sent")
        except Exception:
            # Provider accepted the message. Leave sending to become UNKNOWN after timeout;
            # never turn a post-send DB error into a blind automatic retry.
            logger.warning(
                "[notify-subs] sub=%d outcome commit failed; reconciliation needed", sub.id
            )
            stats.errors += 1
            continue
        stats.subscriptions_notified += 1
        stats.listings_sent += len(views)

    async with sitedb.read_session(database_url=database_url) as session:
        outcomes = list((await session.execute(select(SubscriptionDelivery.status).where(
            SubscriptionDelivery.subscription_id.in_([sub.id for sub in subs])
        ))).scalars())
    stats.pending = sum(status in {"pending", "failed", "sending"} for status in outcomes)
    stats.unknown = outcomes.count("unknown")
    logger.info(
        "[notify-subs] considered=%d notified=%d listings=%d errors=%d",
        stats.subscriptions_considered, stats.subscriptions_notified,
        stats.listings_sent, stats.errors,
    )
    return stats
