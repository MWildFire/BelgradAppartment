"""C2 — the nightly saved-search matcher: digest grouping, idempotency, fault isolation, channels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import (
    Base,
    InboundPost,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SiteListing,
    SourceChannel,
    Subscription,
    SubscriptionMatch,
)
from src.jobs.notify_subscriptions import notify_subscriptions


def _dt(hours_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours_ago)


async def _seed(factory: async_sessionmaker) -> None:
    """Two public listings in Врачар (one cheap, one pricey) + Земун, all fresh."""
    async with factory() as s:
        s.add(SourceChannel(id=1, tg_id=111))
        await s.flush()
        specs = [
            (1, "Vracar", 600, 2.0),
            (2, "Vracar", 1500, 3.0),
            (3, "Zemun", 700, 1.0),
        ]
        for lid, district, price, rooms in specs:
            s.add(
                Listing(
                    id=lid, platform=Platform.NEKRETNINE, external_id=f"e{lid}",
                    url=f"http://x/{lid}", price_eur=price, area_m2=40.0, rooms=rooms,
                    district=district, photo_urls=[f"http://cdn/{lid}.jpg"],
                )
            )
            s.add(
                InboundPost(
                    id=lid, source_channel_id=1, tg_message_id=100 + lid, raw_text="",
                    photo_paths=[f"data/inbound/{lid}/00.jpg"],
                )
            )
        await s.flush()
        for mid in (1, 2, 3):
            s.add(Match(id=mid, inbound_post_id=mid, listing_id=mid, status=MatchStatus.PUBLISHED))
        await s.flush()
        for sid in (1, 2, 3):
            s.add(
                SiteListing(
                    id=sid, match_id=sid, is_public=True, created_at=_dt(2), updated_at=_dt(2)
                )
            )
        await s.commit()


@pytest_asyncio.fixture
async def db_url(tmp_path: Path):
    db_path = tmp_path / "notify.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _seed(factory)
    await engine.dispose()
    yield url


async def _add_sub(url: str, **kw) -> int:
    from src.site import db as sitedb

    async with sitedb.writer_session(database_url=url) as s:
        sub = Subscription(
            criteria=kw.pop("criteria", {}),
            channel=kw.pop("channel", "email"),
            email=kw.pop("email", "a@b.com"),
            telegram_chat_id=kw.pop("telegram_chat_id", None),
            token=kw.pop("token", "tok"),
            is_active=kw.pop("is_active", True),
        )
        s.add(sub)
        await s.flush()
        await s.refresh(sub)
        return sub.id


async def _matches(url: str) -> list[tuple[int, int]]:
    from src.site import db as sitedb

    async with sitedb.read_session(database_url=url) as s:
        rows = (
            await s.execute(
                select(SubscriptionMatch.subscription_id, SubscriptionMatch.site_listing_id)
                .order_by(SubscriptionMatch.subscription_id, SubscriptionMatch.site_listing_id)
            )
        ).all()
        return [(r[0], r[1]) for r in rows]


# ----------------------------------------------------------- digest grouping + criteria match


@pytest.mark.asyncio
async def test_one_digest_per_subscription_groups_matching_listings(db_url, monkeypatch) -> None:
    # Sub wants Врачар up to 800€ → matches listing 1 (600) only, not listing 2 (1500) or 3 (Zemun).
    await _add_sub(
        db_url, token="t1",
        criteria={"district": ["Врачар"], "price_max": 800, "vnj": False, "wc": False,
                  "rooms": [], "price_min": None},
    )
    sent: list = []
    monkeypatch.setattr(
        "src.jobs.notify_subscriptions.send_subscription_digest",
        _capture_email(sent),
    )
    stats = await notify_subscriptions(database_url=db_url)
    assert stats.subscriptions_notified == 1
    assert stats.listings_sent == 1
    # exactly one digest call, with the one matching listing
    assert len(sent) == 1
    assert len(sent[0]["listings"]) == 1
    assert await _matches(db_url) == [(1, 1)]  # (sub 1, site_listing 1)


@pytest.mark.asyncio
async def test_digest_groups_multiple_new_listings_into_one_notice(db_url, monkeypatch) -> None:
    # Sub wants Врачар (no price cap) → BOTH Врачар listings (1 and 2), one digest.
    await _add_sub(
        db_url, token="t1",
        criteria={"district": ["Врачар"], "price_max": None, "price_min": None,
                  "vnj": False, "wc": False, "rooms": []},
    )
    sent: list = []
    monkeypatch.setattr(
        "src.jobs.notify_subscriptions.send_subscription_digest", _capture_email(sent)
    )
    stats = await notify_subscriptions(database_url=db_url)
    assert len(sent) == 1  # ONE notice
    assert len(sent[0]["listings"]) == 2  # both grouped
    assert stats.listings_sent == 2
    assert await _matches(db_url) == [(1, 1), (1, 2)]


# ----------------------------------------------------------- idempotency


@pytest.mark.asyncio
async def test_second_run_sends_nothing_new(db_url, monkeypatch) -> None:
    await _add_sub(
        db_url, token="t1",
        criteria={"district": ["Врачар"], "price_max": None, "price_min": None,
                  "vnj": False, "wc": False, "rooms": []},
    )
    sent: list = []
    monkeypatch.setattr(
        "src.jobs.notify_subscriptions.send_subscription_digest", _capture_email(sent)
    )
    await notify_subscriptions(database_url=db_url)
    assert len(sent) == 1
    sent.clear()
    stats2 = await notify_subscriptions(database_url=db_url)
    assert sent == []  # nothing re-sent
    assert stats2.subscriptions_notified == 0
    assert stats2.listings_sent == 0
    assert await _matches(db_url) == [(1, 1), (1, 2)]  # ledger unchanged


# ----------------------------------------------------------- since_hours window


@pytest.mark.asyncio
async def test_only_recent_listings_within_since_hours(db_url, monkeypatch) -> None:
    # Age listing 1's site row beyond the window; listing 2 stays fresh.
    from src.site import db as sitedb

    async with sitedb.writer_session(database_url=db_url) as s:
        from sqlalchemy import update

        await s.execute(
            update(SiteListing).where(SiteListing.id == 1).values(created_at=_dt(50))
        )
    await _add_sub(
        db_url, token="t1",
        criteria={"district": ["Врачар"], "price_max": None, "price_min": None,
                  "vnj": False, "wc": False, "rooms": []},
    )
    sent: list = []
    monkeypatch.setattr(
        "src.jobs.notify_subscriptions.send_subscription_digest", _capture_email(sent)
    )
    await notify_subscriptions(database_url=db_url, since_hours=24)
    assert len(sent) == 1
    assert [v.id for v in sent[0]["listings"]] == [2]  # only the fresh one


# ----------------------------------------------------------- inactive / unlinked skipped


@pytest.mark.asyncio
async def test_inactive_subscription_gets_nothing(db_url, monkeypatch) -> None:
    await _add_sub(
        db_url, token="t1", is_active=False,
        criteria={"district": ["Врачар"], "price_max": None, "price_min": None,
                  "vnj": False, "wc": False, "rooms": []},
    )
    sent: list = []
    monkeypatch.setattr(
        "src.jobs.notify_subscriptions.send_subscription_digest", _capture_email(sent)
    )
    stats = await notify_subscriptions(database_url=db_url)
    assert sent == []
    assert stats.subscriptions_notified == 0


@pytest.mark.asyncio
async def test_unlinked_telegram_subscription_gets_nothing(db_url, monkeypatch) -> None:
    # is_active True (defensive) but telegram channel with no chat_id → cannot deliver, skip.
    await _add_sub(
        db_url, token="t1", channel="telegram", email=None, telegram_chat_id=None,
        is_active=True,
        criteria={"district": ["Врачар"], "price_max": None, "price_min": None,
                  "vnj": False, "wc": False, "rooms": []},
    )
    tg_sent: list = []
    monkeypatch.setattr(
        "src.jobs.notify_subscriptions.send_subscription_digest_telegram",
        _capture_tg(tg_sent),
    )
    stats = await notify_subscriptions(database_url=db_url)
    assert tg_sent == []
    assert stats.subscriptions_notified == 0
    assert await _matches(db_url) == []  # no ledger rows when nothing delivered


# ----------------------------------------------------------- channels


@pytest.mark.asyncio
async def test_telegram_channel_sends_to_chat_id(db_url, monkeypatch) -> None:
    await _add_sub(
        db_url, token="t1", channel="telegram", email=None, telegram_chat_id=555,
        is_active=True,
        criteria={"district": ["Врачар"], "price_max": 800, "price_min": None,
                  "vnj": False, "wc": False, "rooms": []},
    )
    tg_sent: list = []
    monkeypatch.setattr(
        "src.jobs.notify_subscriptions.send_subscription_digest_telegram",
        _capture_tg(tg_sent),
    )
    stats = await notify_subscriptions(database_url=db_url)
    assert len(tg_sent) == 1
    assert tg_sent[0]["chat_id"] == 555
    assert stats.subscriptions_notified == 1
    assert await _matches(db_url) == [(1, 1)]


# ----------------------------------------------------------- fault isolation


@pytest.mark.asyncio
async def test_one_bad_send_does_not_abort_the_batch(db_url, monkeypatch) -> None:
    # Two e-mail subs; the first send raises, the second must still go and be recorded.
    await _add_sub(
        db_url, token="boom", email="boom@x.com",
        criteria={"district": ["Врачар"], "price_max": 800, "price_min": None,
                  "vnj": False, "wc": False, "rooms": []},
    )
    await _add_sub(
        db_url, token="ok", email="ok@x.com",
        criteria={"district": ["Земун"], "price_max": None, "price_min": None,
                  "vnj": False, "wc": False, "rooms": []},
    )
    delivered: list = []

    async def flaky(email, listings, unsubscribe_url):
        if email == "boom@x.com":
            raise RuntimeError("smtp down")
        delivered.append(email)
        return True

    monkeypatch.setattr("src.jobs.notify_subscriptions.send_subscription_digest", flaky)
    stats = await notify_subscriptions(database_url=db_url)
    assert delivered == ["ok@x.com"]  # the healthy one still delivered
    assert stats.errors == 1
    assert stats.subscriptions_notified == 1
    # Only the successful sub got a ledger row (the failed one is retried next run).
    ms = await _matches(db_url)
    assert all(sub_id != 1 or True for sub_id, _ in ms)  # boom sub has no rows
    # boom sub id is 1; ensure it recorded nothing.
    assert not any(sub_id == 1 for sub_id, _ in ms)


@pytest.mark.asyncio
async def test_dry_run_sends_and_records_nothing(db_url, monkeypatch) -> None:
    await _add_sub(
        db_url, token="t1",
        criteria={"district": ["Врачар"], "price_max": None, "price_min": None,
                  "vnj": False, "wc": False, "rooms": []},
    )
    sent: list = []
    monkeypatch.setattr(
        "src.jobs.notify_subscriptions.send_subscription_digest", _capture_email(sent)
    )
    stats = await notify_subscriptions(database_url=db_url, dry_run=True)
    assert sent == []  # dry-run never sends
    assert await _matches(db_url) == []  # nor records
    assert stats.listings_sent == 2  # but reports what WOULD be sent


@pytest.mark.asyncio
async def test_record_is_idempotent_on_duplicate(db_url) -> None:
    """Review fix (critical): the ledger insert is ON CONFLICT DO NOTHING, so a concurrent run
    or a retry recording the same (subscription, listing) must NOT raise — otherwise the
    IntegrityError would escape the per-subscription try and abort the whole nightly batch."""
    from src.jobs.notify_subscriptions import _record

    sub_id = await _add_sub(db_url, token="dup")
    await _record(db_url, sub_id, [1, 2])
    # Exact duplicate — would be a UNIQUE violation without on_conflict_do_nothing.
    await _record(db_url, sub_id, [1, 2])

    assert sorted(await _matches(db_url)) == [(sub_id, 1), (sub_id, 2)]  # one row each, no dupes


# ----------------------------------------------------------- capture helpers


def _capture_email(sink: list):
    async def _send(email, listings, unsubscribe_url):
        sink.append({"email": email, "listings": list(listings), "unsub": unsubscribe_url})
        return True

    return _send


def _capture_tg(sink: list):
    async def _send(chat_id, listings, unsubscribe_url):
        sink.append({"chat_id": chat_id, "listings": list(listings), "unsub": unsubscribe_url})
        return True

    return _send
