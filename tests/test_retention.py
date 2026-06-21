"""Stage W2 follow-up — site-listing retention (warn-first 3-week expiry notice)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from src.db.models import (
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SiteListing,
    SourceChannel,
    utcnow,
)
from src.site import retention
from tests.conftest import fresh_db


@pytest_asyncio.fixture
async def db():
    async with fresh_db() as (_engine, factory):
        yield factory


async def _seed(db, *, is_public: bool, age_days: int, price: int = 900, ext: str = "r1") -> int:
    uid = sum((i + 1) * ord(c) for i, c in enumerate(ext))  # deterministic, unique per ext
    async with db() as s:
        ch = SourceChannel(tg_id=-(2001 + uid), username=f"rsrc-{ext}", is_active=True)
        s.add(ch)
        await s.flush()
        lst = Listing(
            platform=Platform.NEKRETNINE, external_id=ext, url=f"https://x/{ext}",
            price_eur=price, district="Vracar", photo_urls=["u"], photo_phashes=["00"],
        )
        ib = InboundPost(
            source_channel_id=ch.id, tg_message_id=1, raw_text="",
            photo_paths=["/tmp/a.jpg"], photo_phashes=["00"], status=InboundStatus.MATCHED,
        )
        s.add_all([lst, ib])
        await s.flush()
        m = Match(
            inbound_post_id=ib.id, listing_id=lst.id, confidence=0.7,
            matched_photo_count=1, method="phash", status=MatchStatus.PUBLISHED,
        )
        s.add(m)
        await s.flush()
        sl = SiteListing(
            match_id=m.id, is_public=is_public, created_at=utcnow() - timedelta(days=age_days),
        )
        s.add(sl)
        await s.flush()
        await s.commit()
        return sl.id


@pytest.mark.asyncio
async def test_overdue_only_old_and_public(db):
    old_pub = await _seed(db, is_public=True, age_days=25, ext="old")
    await _seed(db, is_public=True, age_days=5, ext="fresh")      # too new
    await _seed(db, is_public=False, age_days=40, ext="hidden")   # not on the site
    overdue = await retention.overdue_listings()
    assert [o.site_listing_id for o in overdue] == [old_pub]
    assert overdue[0].age_days >= retention.RETENTION_DAYS


@pytest.mark.asyncio
async def test_send_notice_calls_bot_with_overdue(db, monkeypatch):
    from src.config import get_settings

    await _seed(db, is_public=True, age_days=30, price=1234, ext="o1")
    monkeypatch.setattr(get_settings(), "tg_bot_token", "test-token")
    sent = {}

    async def fake_send(text):  # send_text_via_bot(text) — bot-only, no client arg
        sent["text"] = text

    monkeypatch.setattr(retention, "send_text_via_bot", fake_send)
    n = await retention.send_overdue_notice()
    assert n == 1
    assert "1234€" in sent["text"] and "пора убрать" in sent["text"]


@pytest.mark.asyncio
async def test_send_notice_noop_when_none(db, monkeypatch):
    calls = []

    async def fake_send(text):
        calls.append(text)

    monkeypatch.setattr(retention, "send_text_via_bot", fake_send)
    assert await retention.send_overdue_notice() == 0
    assert calls == []


@pytest.mark.asyncio
async def test_send_notice_noop_without_bot_token(db, monkeypatch):
    """Overdue exist but TG_BOT_TOKEN unset → no crash, no send, returns 0."""
    from src.config import get_settings

    await _seed(db, is_public=True, age_days=30, ext="o2")
    monkeypatch.setattr(get_settings(), "tg_bot_token", "")
    calls = []

    async def fake_send(text):
        calls.append(text)

    monkeypatch.setattr(retention, "send_text_via_bot", fake_send)
    assert await retention.send_overdue_notice() == 0
    assert calls == []
