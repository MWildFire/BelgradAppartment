"""F18/F19: transport confirmation and a durable backlog survive outages and caps."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from src.config import get_settings
from src.db.models import (
    InboundPost,
    Listing,
    Match,
    Platform,
    SiteListing,
    Subscription,
    SubscriptionDelivery,
)
from src.jobs import notify_subscriptions as notify
from src.site import db as sitedb
from tests import test_notify_subscriptions as fixtures
from tests.test_notify_subscriptions import _add_sub, _matches

db_url = fixtures.db_url


async def _many(url, count=100):
    now = datetime.now(UTC) - timedelta(hours=1)
    async with sitedb.writer_session(database_url=url) as session:
        await session.execute(update(SiteListing).values(is_public=False))
        for lid in range(10, 10 + count):
            session.add(Listing(id=lid, platform=Platform.NEKRETNINE, external_id=str(lid), url=f"https://example.invalid/{lid}", price_eur=600))
            session.add(InboundPost(id=lid, source_channel_id=1, tg_message_id=1000 + lid, raw_text="", photo_paths=[]))
        await session.flush()
        for lid in range(10, 10 + count):
            session.add(Match(id=lid, listing_id=lid, inbound_post_id=lid))
        await session.flush()
        for lid in range(10, 10 + count):
            session.add(SiteListing(id=lid, match_id=lid, is_public=True, created_at=now))


async def _queue(url):
    async with sitedb.read_session(database_url=url) as session:
        return list((await session.execute(select(SubscriptionDelivery))).scalars())


@pytest.mark.asyncio
async def test_missing_transport_does_not_claim_sent(db_url, monkeypatch):
    await _add_sub(db_url)
    monkeypatch.setattr(get_settings(), "site_smtp_host", "")
    stats = await notify.notify_subscriptions(database_url=db_url)
    assert stats.listings_sent == stats.subscriptions_notified == 0
    assert stats.errors == 1
    assert await _matches(db_url) == []
    assert {row.status for row in await _queue(db_url)} == {"failed"}


@pytest.mark.asyncio
async def test_100_matches_survive_72_hour_outage_then_drain_30_30_30_10(db_url, monkeypatch):
    await _many(db_url)
    await _add_sub(db_url)
    monkeypatch.setattr(get_settings(), "site_smtp_host", "")
    await notify.notify_subscriptions(database_url=db_url)
    assert len(await _queue(db_url)) == 100
    future = datetime.now(UTC) + timedelta(hours=72)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return future
    monkeypatch.setattr(notify, "datetime", Later)
    sent = []
    async def confirmed(email, listings, url):
        sent.extend(view.id for view in listings)
        return True
    monkeypatch.setattr(notify, "send_subscription_digest", confirmed)
    counts = [(await notify.notify_subscriptions(database_url=db_url)).listings_sent for _ in range(4)]
    assert counts == [30, 30, 30, 10]
    assert len(sent) == len(set(sent)) == len(await _matches(db_url)) == 100
    assert {row.status for row in await _queue(db_url)} == {"sent"}


@pytest.mark.asyncio
async def test_discovery_cursor_catches_new_cards_after_scheduler_outage(db_url, monkeypatch):
    await _many(db_url, 4)
    sub_id = await _add_sub(db_url)
    async with sitedb.writer_session(database_url=db_url) as session:
        await session.execute(update(Subscription).where(Subscription.id == sub_id).values(notification_cursor_at=datetime.now(UTC) - timedelta(hours=96)))
        await session.execute(update(SiteListing).values(created_at=datetime.now(UTC) - timedelta(hours=72)))
    async def confirmed(*args):
        return True
    monkeypatch.setattr(notify, "send_subscription_digest", confirmed)
    assert (await notify.notify_subscriptions(database_url=db_url)).listings_sent == 4


@pytest.mark.asyncio
async def test_timeout_after_send_is_unknown_and_not_replayed(db_url, monkeypatch):
    await _add_sub(db_url)
    calls = []
    async def accepted_then_timeout(*args):
        calls.append(1)
        raise TimeoutError("accepted; response lost")
    monkeypatch.setattr(notify, "send_subscription_digest", accepted_then_timeout)
    await notify.notify_subscriptions(database_url=db_url)
    await notify.notify_subscriptions(database_url=db_url)
    assert calls == [1]
    assert {row.status for row in await _queue(db_url)} == {"unknown"}
    assert await _matches(db_url) == []


@pytest.mark.asyncio
async def test_concurrent_runs_do_not_send_same_queue_item_twice(db_url, monkeypatch):
    await _add_sub(db_url)
    sent = []
    async def confirmed(email, views, url):
        await asyncio.sleep(0)
        sent.extend(view.id for view in views)
        return True
    monkeypatch.setattr(notify, "send_subscription_digest", confirmed)
    await asyncio.gather(notify.notify_subscriptions(database_url=db_url), notify.notify_subscriptions(database_url=db_url))
    assert len(sent) == len(set(sent)) == 3
    assert len(await _matches(db_url)) == 3


@pytest.mark.asyncio
async def test_dry_run_does_not_advance_cursor_or_queue(db_url):
    await _add_sub(db_url)
    stats = await notify.notify_subscriptions(database_url=db_url, dry_run=True)
    assert stats.listings_sent == 3
    assert await _queue(db_url) == []
    async with sitedb.read_session(database_url=db_url) as session:
        assert (await session.execute(select(Subscription.notification_cursor_at))).scalar_one() is None
