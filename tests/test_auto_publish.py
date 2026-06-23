"""X1 — auto-publish: a delivered match auto-appears on the public site.

Covers the shared `auto_publish_site_listing` helper (single source of truth for
create-time flag defaulting), the delivery-path hook (fault-isolated + kill-switched),
and the nightly backfill `auto_publish_new_listings`.

DB setup mirrors tests/test_admin_publish.py: a fresh in-memory SQLite swapped into the
global session factory via conftest.fresh_db.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from src.admin.publish import auto_publish_site_listing
from src.db import get_session
from src.db.models import (
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    PublishedPost,
    SiteListing,
    SourceChannel,
)
from tests.conftest import fresh_db


@pytest_asyncio.fixture
async def db():
    async with fresh_db() as (_engine, factory):
        yield factory


async def _seed_match(
    db,
    *,
    status: MatchStatus = MatchStatus.PENDING,
    raw_text: str = "",
    external_id: str = "ext1",
    tg_message_id: int = 42,
    created_at: datetime | None = None,
    with_published: bool = False,
) -> int:
    async with db() as s:
        ch = SourceChannel(tg_id=-(1000 + tg_message_id), username=f"chan{tg_message_id}", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id=external_id,
            url=f"https://nekretnine.rs/{external_id}",
            price_eur=850,
            photo_urls=["u1"],
            photo_phashes=["00"],
        )
        ib = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=tg_message_id,
            raw_text=raw_text,
            photo_paths=["/tmp/a.jpg"],
            photo_phashes=["00"],
            status=InboundStatus.MATCHED,
        )
        s.add_all([listing, ib])
        await s.flush()
        m = Match(
            inbound_post_id=ib.id,
            listing_id=listing.id,
            confidence=0.75,
            matched_photo_count=1,
            method="phash",
            status=status,
        )
        if created_at is not None:
            m.created_at = created_at
        s.add(m)
        await s.flush()
        if with_published:
            s.add(PublishedPost(match_id=m.id, tg_message_id=tg_message_id, post_text="x"))
        await s.commit()
        return m.id


async def _site_listing(db, match_id: int) -> SiteListing | None:
    async with db() as s:
        return (
            await s.execute(select(SiteListing).where(SiteListing.match_id == match_id))
        ).scalar_one_or_none()


async def _site_listing_count(db) -> int:
    async with db() as s:
        return (await s.execute(select(func.count()).select_from(SiteListing))).scalar_one()


# --------------------------------------------------------------------------- helper


@pytest.mark.asyncio
async def test_auto_publish_creates_public_listing_with_parsed_flags(db):
    match_id = await _seed_match(db, raw_text="Сдаём под ВНЖ, поможем с боравком")
    async with db() as s:
        row, created = await auto_publish_site_listing(s, match_id)
        await s.commit()
    assert created is True
    assert row.is_public is True
    assert row.is_for_vnj is True
    assert row.is_for_white_card is False


@pytest.mark.asyncio
async def test_auto_publish_idempotent_preserves_manual_curation(db):
    match_id = await _seed_match(db, raw_text="под ВНЖ")
    # Дарья already curated: hidden, white-card on, vnj off
    async with db() as s:
        s.add(
            SiteListing(
                match_id=match_id,
                is_public=False,
                is_for_vnj=False,
                is_for_white_card=True,
                note="ручная заметка",
            )
        )
        await s.commit()
    async with db() as s:
        row, created = await auto_publish_site_listing(s, match_id)
        await s.commit()
    assert created is False
    sl = await _site_listing(db, match_id)
    assert sl.is_public is False
    assert sl.is_for_vnj is False
    assert sl.is_for_white_card is True
    assert sl.note == "ручная заметка"
    assert await _site_listing_count(db) == 1


# --------------------------------------------------------------------------- config flags


def test_auto_sync_settings_default_on():
    from src.config import Settings

    s = Settings()
    assert s.auto_publish_on_send is True
    assert s.nightly_auto_publish is True
    assert s.nightly_auto_hide_site_listings is True


# --------------------------------------------------------------------------- cache invalidation


def test_invalidate_catalog_cache_clears_all_three_caches():
    """In-process invalidation must drop catalog/facets/map entries so an is_public flip
    is visible immediately within the same process (e.g. tests / same-proc admin)."""
    from src.site import cache

    cache.catalog_cache.set(("k",), "v")
    cache.facets_cache.set(("facets",), "f")
    cache.map_cache.set(("map",), "m")
    assert cache.catalog_cache.get(("k",)) == "v"

    cache.invalidate_catalog_cache()

    assert cache.catalog_cache.get(("k",)) is cache.MISS
    assert cache.facets_cache.get(("facets",)) is cache.MISS
    assert cache.map_cache.get(("map",)) is cache.MISS


@pytest.mark.asyncio
async def test_auto_publish_create_invalidates_cache(db):
    """A new public card must bust the in-process read-model cache so it appears at once."""
    from src.site import cache

    cache.catalog_cache.set(("k",), "v")
    match_id = await _seed_match(db, raw_text="студия")
    async with db() as s:
        await auto_publish_site_listing(s, match_id)
        await s.commit()
    assert cache.catalog_cache.get(("k",)) is cache.MISS


@pytest.mark.asyncio
async def test_auto_publish_no_create_does_not_invalidate_cache(db):
    """Idempotent no-op (row already exists) changes nothing — leave the cache intact."""
    from src.site import cache

    match_id = await _seed_match(db, raw_text="студия")
    async with db() as s:
        s.add(SiteListing(match_id=match_id, is_public=False))
        await s.commit()
    cache.catalog_cache.set(("k",), "v")
    async with db() as s:
        _row, created = await auto_publish_site_listing(s, match_id)
        await s.commit()
    assert created is False
    assert cache.catalog_cache.get(("k",)) == "v"


# --------------------------------------------------------------------------- backfill


@pytest.mark.asyncio
async def test_backfill_exposes_published_match_lacking_site_listing(db):
    from src.scrapers.orchestrator import auto_publish_new_listings

    match_id = await _seed_match(
        db, status=MatchStatus.PUBLISHED, raw_text="под ВНЖ", with_published=True
    )
    n = await auto_publish_new_listings()
    assert n == 1
    sl = await _site_listing(db, match_id)
    assert sl is not None
    assert sl.is_public is True
    assert sl.is_for_vnj is True  # flags parsed on create


@pytest.mark.asyncio
async def test_backfill_skips_match_with_existing_site_listing(db):
    from src.scrapers.orchestrator import auto_publish_new_listings

    match_id = await _seed_match(db, status=MatchStatus.PUBLISHED, with_published=True)
    async with db() as s:
        s.add(SiteListing(match_id=match_id, is_public=False))
        await s.commit()
    n = await auto_publish_new_listings()
    assert n == 0
    sl = await _site_listing(db, match_id)
    assert sl.is_public is False  # untouched
    assert await _site_listing_count(db) == 1


@pytest.mark.asyncio
async def test_backfill_skips_published_match_without_published_post(db):
    """PUBLISHED but no PublishedPost row (e.g. mark-posted only) is out of scope for the
    send-driven backfill — mark-posted already exposes those itself."""
    from src.scrapers.orchestrator import auto_publish_new_listings

    await _seed_match(db, status=MatchStatus.PUBLISHED, with_published=False)
    n = await auto_publish_new_listings()
    assert n == 0
    assert await _site_listing_count(db) == 0


@pytest.mark.asyncio
async def test_backfill_skips_pending_and_rejected(db):
    from src.scrapers.orchestrator import auto_publish_new_listings

    # PENDING with a (hypothetical) published row, and REJECTED — neither should be exposed
    await _seed_match(db, status=MatchStatus.PENDING, with_published=True, tg_message_id=1)
    await _seed_match(
        db, status=MatchStatus.REJECTED, with_published=True, tg_message_id=2, external_id="ext2"
    )
    n = await auto_publish_new_listings()
    assert n == 0
    assert await _site_listing_count(db) == 0


@pytest.mark.asyncio
async def test_backfill_respects_since_hours(db):
    from src.scrapers.orchestrator import auto_publish_new_listings

    old = datetime.now(timezone.utc) - timedelta(hours=48)
    await _seed_match(
        db, status=MatchStatus.PUBLISHED, with_published=True, created_at=old
    )
    n = await auto_publish_new_listings(since_hours=24)
    assert n == 0
    assert await _site_listing_count(db) == 0


# --------------------------------------------------------------------------- bot-delivery hook


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        # aiogram exposes .message_id; Telethon exposes .id — support both transports.
        self.message_id = message_id
        self.id = message_id


class _FakeBotSession:
    async def close(self) -> None:
        return None


class _FakeBot:
    """Minimal aiogram.Bot stand-in: records sends, returns a one-message album."""

    def __init__(self) -> None:
        self.session = _FakeBotSession()
        self.sent: list[int] = []

    async def send_media_group(self, *, chat_id: int, media):  # type: ignore[no-untyped-def]
        self.sent.append(chat_id)
        return [_FakeMessage(555)]


async def _seed_match_with_real_photo(db, tmp_path, *, raw_text="", status=MatchStatus.PENDING):
    """Seed a match whose inbound has a real on-disk photo (bot delivery requires it)."""
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fake")
    async with db() as s:
        ch = SourceChannel(tg_id=-7001, username="srcchan", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.NEKRETNINE, external_id="ext1",
            url="https://nekretnine.rs/x", price_eur=850,
            photo_urls=["u1"], photo_phashes=["00"],
        )
        ib = InboundPost(
            source_channel_id=ch.id, tg_message_id=42, raw_text=raw_text,
            photo_paths=[str(photo)], photo_phashes=["00"], status=InboundStatus.MATCHED,
        )
        s.add_all([listing, ib])
        await s.flush()
        m = Match(
            inbound_post_id=ib.id, listing_id=listing.id, confidence=0.75,
            matched_photo_count=1, method="phash", status=status,
        )
        s.add(m)
        await s.commit()
        return m.id


@pytest.fixture
def _bot_env(monkeypatch):
    """Bot mode with a fake transport + one recipient; auto-publish ON by default."""
    from src.config import get_settings
    from src.telegram import bot_delivery

    s = get_settings()
    monkeypatch.setattr(s, "tg_bot_token", "fake-token")
    monkeypatch.setattr(s, "tg_bot_recipients", "12345")
    monkeypatch.setattr(s, "auto_publish_on_send", True)
    fake = _FakeBot()
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_bot_delivery_success_creates_one_public_listing(db, tmp_path, _bot_env):
    from src.telegram.bot_delivery import send_match_via_bot

    match_id = await _seed_match_with_real_photo(db, tmp_path, raw_text="под ВНЖ")
    pub = await send_match_via_bot(match_id)

    assert pub is not None
    sl = await _site_listing(db, match_id)
    assert sl is not None
    assert sl.is_public is True
    assert sl.is_for_vnj is True
    assert await _site_listing_count(db) == 1
    async with db() as s:
        m = await s.get(Match, match_id)
        assert m.status is MatchStatus.PUBLISHED


@pytest.mark.asyncio
async def test_bot_redelivery_no_second_row_preserves_manual_hide(db, tmp_path, _bot_env):
    from src.telegram.bot_delivery import send_match_via_bot

    match_id = await _seed_match_with_real_photo(db, tmp_path)
    await send_match_via_bot(match_id)
    # Дарья manually hides the card after the first delivery
    async with db() as s:
        sl = (
            await s.execute(select(SiteListing).where(SiteListing.match_id == match_id))
        ).scalar_one()
        sl.is_public = False
        await s.commit()

    await send_match_via_bot(match_id, force_resend=True)

    assert await _site_listing_count(db) == 1
    sl = await _site_listing(db, match_id)
    assert sl.is_public is False  # manual hide preserved


@pytest.mark.asyncio
async def test_bot_delivery_kill_switch_off_creates_no_listing(db, tmp_path, _bot_env, monkeypatch):
    from src.config import get_settings
    from src.telegram.bot_delivery import send_match_via_bot

    monkeypatch.setattr(get_settings(), "auto_publish_on_send", False)
    match_id = await _seed_match_with_real_photo(db, tmp_path, raw_text="под ВНЖ")
    pub = await send_match_via_bot(match_id)

    assert pub is not None  # delivery still succeeded
    assert await _site_listing_count(db) == 0
    async with db() as s:
        m = await s.get(Match, match_id)
        assert m.status is MatchStatus.PUBLISHED


@pytest.mark.asyncio
async def test_bot_delivery_rejected_match_not_auto_published(db, tmp_path, _bot_env):
    from src.telegram.bot_delivery import send_match_via_bot

    # Admin rejected mid-flight; resolve_post_send_status leaves it REJECTED → no card.
    match_id = await _seed_match_with_real_photo(db, tmp_path, status=MatchStatus.REJECTED)
    await send_match_via_bot(match_id)

    assert await _site_listing_count(db) == 0
    async with db() as s:
        m = await s.get(Match, match_id)
        assert m.status is MatchStatus.REJECTED


@pytest.mark.asyncio
async def test_bot_delivery_survives_auto_publish_exception(db, tmp_path, _bot_env, monkeypatch):
    """Fault isolation: an auto-publish helper crash must NOT break the Telegram send."""
    import src.telegram.bot_delivery as bd
    from src.telegram.bot_delivery import send_match_via_bot

    async def _boom(session, match_id):
        raise RuntimeError("auto-publish blew up")

    monkeypatch.setattr(bd, "auto_publish_site_listing", _boom)
    match_id = await _seed_match_with_real_photo(db, tmp_path)
    pub = await send_match_via_bot(match_id)

    assert pub is not None  # send succeeded despite the helper raising
    async with db() as s:
        m = await s.get(Match, match_id)
        assert m.status is MatchStatus.PUBLISHED
    assert await _site_listing_count(db) == 0


# --------------------------------------------------------------------------- userbot-delivery hook


class _FakeUserbotClient:
    """Minimal Telethon client stand-in for _send_match_via_userbot."""

    async def get_me(self):  # type: ignore[no-untyped-def]
        return type("Me", (), {"username": "darya"})()

    async def send_file(self, recipient, *, file, caption):  # type: ignore[no-untyped-def]
        return [_FakeMessage(777)]


@pytest.fixture
def _userbot_env(monkeypatch):
    from src.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "tg_delivery_target", "me")
    monkeypatch.setattr(s, "auto_publish_on_send", True)


@pytest.mark.asyncio
async def test_userbot_delivery_success_creates_one_public_listing(db, tmp_path, _userbot_env):
    from src.telegram.delivery import _send_match_via_userbot

    match_id = await _seed_match_with_real_photo(db, tmp_path, raw_text="под ВНЖ")
    pub = await _send_match_via_userbot(match_id, _FakeUserbotClient())

    assert pub is not None
    sl = await _site_listing(db, match_id)
    assert sl is not None and sl.is_public is True and sl.is_for_vnj is True
    assert await _site_listing_count(db) == 1
    async with db() as s:
        assert (await s.get(Match, match_id)).status is MatchStatus.PUBLISHED


@pytest.mark.asyncio
async def test_userbot_delivery_kill_switch_off(db, tmp_path, _userbot_env, monkeypatch):
    from src.config import get_settings
    from src.telegram.delivery import _send_match_via_userbot

    monkeypatch.setattr(get_settings(), "auto_publish_on_send", False)
    match_id = await _seed_match_with_real_photo(db, tmp_path, raw_text="под ВНЖ")
    pub = await _send_match_via_userbot(match_id, _FakeUserbotClient())

    assert pub is not None
    assert await _site_listing_count(db) == 0


@pytest.mark.asyncio
async def test_userbot_delivery_rejected_not_auto_published(db, tmp_path, _userbot_env):
    from src.telegram.delivery import _send_match_via_userbot

    match_id = await _seed_match_with_real_photo(db, tmp_path, status=MatchStatus.REJECTED)
    await _send_match_via_userbot(match_id, _FakeUserbotClient())

    assert await _site_listing_count(db) == 0
    async with db() as s:
        assert (await s.get(Match, match_id)).status is MatchStatus.REJECTED


@pytest.mark.asyncio
async def test_userbot_delivery_survives_auto_publish_exception(db, tmp_path, _userbot_env, monkeypatch):
    import src.telegram.delivery as dl
    from src.telegram.delivery import _send_match_via_userbot

    async def _boom(session, match_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(dl, "auto_publish_site_listing", _boom)
    match_id = await _seed_match_with_real_photo(db, tmp_path)
    pub = await _send_match_via_userbot(match_id, _FakeUserbotClient())

    assert pub is not None
    async with db() as s:
        assert (await s.get(Match, match_id)).status is MatchStatus.PUBLISHED
    assert await _site_listing_count(db) == 0
