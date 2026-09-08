"""Durable subscription discovery and claims, independent of delivery availability."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.db.models import Subscription, SubscriptionDelivery, SubscriptionMatch
from src.site import db as sitedb


async def enqueue(database_url, subscription_id: int, ids: list[int], now: datetime) -> None:
    async with sitedb.writer_session(database_url=database_url) as session:
        if ids:
            await session.execute(sqlite_insert(SubscriptionDelivery).values([
                {"subscription_id": subscription_id, "site_listing_id": sl_id,
                 "status": "pending", "attempts": 0, "created_at": now, "updated_at": now}
                for sl_id in ids
            ]).on_conflict_do_nothing(index_elements=["subscription_id", "site_listing_id"]))
        await session.execute(update(Subscription).where(
            Subscription.id == subscription_id,
            or_(Subscription.notification_cursor_at.is_(None),
                Subscription.notification_cursor_at < now),
        ).values(notification_cursor_at=now))
        await session.execute(update(SubscriptionDelivery).where(
            SubscriptionDelivery.subscription_id == subscription_id,
            SubscriptionDelivery.status == "sending",
            SubscriptionDelivery.claimed_at < now - timedelta(minutes=15),
        ).values(status="unknown", error_code="interrupted_send", updated_at=now))


async def pending_ids(database_url, subscription_id: int) -> list[int]:
    async with sitedb.read_session(database_url=database_url) as session:
        return list((await session.execute(select(SubscriptionDelivery.site_listing_id).where(
            SubscriptionDelivery.subscription_id == subscription_id,
            SubscriptionDelivery.status.in_(["pending", "failed"]),
        ).order_by(SubscriptionDelivery.id))).scalars())


async def claim(database_url, subscription_id: int, ids: list[int]) -> tuple[str, list[int]]:
    token = uuid4().hex
    now = datetime.now(UTC)
    async with sitedb.writer_session(database_url=database_url) as session:
        claimed = list((await session.execute(update(SubscriptionDelivery).where(
            SubscriptionDelivery.subscription_id == subscription_id,
            SubscriptionDelivery.site_listing_id.in_(ids),
            SubscriptionDelivery.status.in_(["pending", "failed"]),
        ).values(status="sending", claim_token=token, claimed_at=now, updated_at=now,
                 attempts=SubscriptionDelivery.attempts + 1)
          .returning(SubscriptionDelivery.site_listing_id))).scalars())
    return token, claimed


async def finish(database_url, subscription_id: int, token: str, *, status: str,
                 error_code: str | None = None) -> None:
    now = datetime.now(UTC)
    async with sitedb.writer_session(database_url=database_url) as session:
        owned = list((await session.execute(select(SubscriptionDelivery.site_listing_id).where(
            SubscriptionDelivery.subscription_id == subscription_id,
            SubscriptionDelivery.status == "sending", SubscriptionDelivery.claim_token == token,
        ))).scalars())
        if status == "sent" and owned:
            await session.execute(sqlite_insert(SubscriptionMatch).values([
                {"subscription_id": subscription_id, "site_listing_id": sl_id, "notified_at": now}
                for sl_id in owned
            ]).on_conflict_do_nothing(index_elements=["subscription_id", "site_listing_id"]))
        await session.execute(update(SubscriptionDelivery).where(
            SubscriptionDelivery.subscription_id == subscription_id,
            SubscriptionDelivery.status == "sending", SubscriptionDelivery.claim_token == token,
        ).values(status=status, claim_token=None, updated_at=now, error_code=error_code,
                 delivered_at=now if status == "sent" else None))


async def reconcile_unknown(
    database_url: str | None, subscription_id: int, ids: list[int], *, confirmed_sent: bool,
) -> int:
    """Operator-only resolution after verifying the transport/recipient outcome.

    confirmed_sent records the historical fact without sending. A verified non-send releases
    unknown rows to failed for the usual queued retry; scheduled jobs never call this helper.
    """
    now = datetime.now(UTC)
    async with sitedb.writer_session(database_url=database_url) as session:
        resolved = list((await session.execute(update(SubscriptionDelivery).where(
            SubscriptionDelivery.subscription_id == subscription_id,
            SubscriptionDelivery.site_listing_id.in_(ids),
            SubscriptionDelivery.status == "unknown",
        ).values(status="sent" if confirmed_sent else "failed", claim_token=None,
                 error_code="operator_reconciled", updated_at=now,
                 delivered_at=now if confirmed_sent else None)
          .returning(SubscriptionDelivery.site_listing_id))).scalars())
        if confirmed_sent and resolved:
            await session.execute(sqlite_insert(SubscriptionMatch).values([
                {"subscription_id": subscription_id, "site_listing_id": sl_id, "notified_at": now}
                for sl_id in resolved
            ]).on_conflict_do_nothing(index_elements=["subscription_id", "site_listing_id"]))
    return len(resolved)
