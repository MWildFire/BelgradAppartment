"""Stage W · W2-A1 — admin publish-to-site action: SiteListing upsert + relocation-flag parse.

Reuses the CSRF/TestClient/_seed_match plumbing pattern from test_admin_actions.py.
"""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.admin.app import create_app
from src.admin.csrf import CSRF_TOKEN
from src.admin.publish import parse_relocation_flags
from src.db.models import (
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SiteListing,
    SourceChannel,
)
from tests.conftest import fresh_db

CREDS = base64.b64encode(b"darya:test_secret_123").decode()
AUTH = {"Authorization": f"Basic {CREDS}"}
CSRF = {"X-CSRF-Token": CSRF_TOKEN}


@pytest.fixture(autouse=True)
def _force_admin_password(monkeypatch):
    from src.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "admin_username", "darya")
    monkeypatch.setattr(s, "admin_password", "test_secret_123")
    yield


@pytest_asyncio.fixture
async def db():
    async with fresh_db() as (_engine, factory):
        yield factory


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


async def _seed_match(
    db,
    *,
    status: MatchStatus = MatchStatus.PENDING,
    raw_text: str = "",
) -> int:
    async with db() as s:
        ch = SourceChannel(tg_id=-1001, username="srcchan", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id="ext1",
            url="https://nekretnine.rs/x",
            price_eur=850,
            photo_urls=["u1"],
            photo_phashes=["00"],
        )
        ib = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=42,
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
        s.add(m)
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


# --------------------------------------------------------------------------- parse_relocation_flags


@pytest.mark.parametrize(
    "text",
    [
        "Квартира под ВНЖ, всё оформим",
        "moze boravak",
        "помощь с регистрацией",
        "privremeni boravak za vlasnika",
        "Привремени боравак включён",
        "prijava boravka included",
        "пријава боравка",
    ],
)
def test_parse_vnj_positive(text):
    is_vnj, _ = parse_relocation_flags(text)
    assert is_vnj is True


@pytest.mark.parametrize(
    "text",
    [
        "бели картон без проблем",
        "beli karton included",
        "белый картон оформим",
        "white card help",
        "пријава адресе",
        "prijava adrese moguca",
    ],
)
def test_parse_white_card_positive(text):
    _, is_wc = parse_relocation_flags(text)
    assert is_wc is True


@pytest.mark.parametrize("text", ["", None, "Уютная студия в центре, без животных"])
def test_parse_neutral_is_both_false(text):
    assert parse_relocation_flags(text) == (False, False)


def test_parse_is_case_and_diacritics_tolerant():
    # uppercase + a Latin keyword embedded in a longer sentence
    assert parse_relocation_flags("BORAVAK i BELI KARTON") == (True, True)


# --------------------------------------------------------------------------- mark-posted upsert


@pytest.mark.asyncio
async def test_mark_posted_creates_public_site_listing_with_parsed_flags(client, db):
    match_id = await _seed_match(db, raw_text="Сдаём под ВНЖ, поможем с боравком")
    r = client.post(f"/match/{match_id}/mark-posted", headers={**AUTH, **CSRF})
    assert r.status_code == 200
    sl = await _site_listing(db, match_id)
    assert sl is not None
    assert sl.is_public is True
    assert sl.is_for_vnj is True
    assert sl.is_for_white_card is False


@pytest.mark.asyncio
async def test_mark_posted_neutral_text_flags_false_still_public(client, db):
    match_id = await _seed_match(db, raw_text="Светлая студия, кухня-гостиная")
    client.post(f"/match/{match_id}/mark-posted", headers={**AUTH, **CSRF})
    sl = await _site_listing(db, match_id)
    assert sl is not None
    assert sl.is_public is True
    assert sl.is_for_vnj is False
    assert sl.is_for_white_card is False


@pytest.mark.asyncio
async def test_mark_posted_preserves_manual_overrides(client, db):
    match_id = await _seed_match(db, raw_text="под ВНЖ")
    # Дарья already curated this listing: hidden, white-card on, vnj off
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
    r = client.post(f"/match/{match_id}/mark-posted", headers={**AUTH, **CSRF})
    assert r.status_code == 200
    sl = await _site_listing(db, match_id)
    # re-marking must NOT clobber manual flags/is_public/note
    assert sl.is_public is False
    assert sl.is_for_vnj is False
    assert sl.is_for_white_card is True
    assert sl.note == "ручная заметка"
    assert await _site_listing_count(db) == 1  # upsert, not a second row


# --------------------------------------------------------------------------- toggle routes / CSRF


@pytest.mark.parametrize(
    "route",
    [
        "site/toggle-public",
        "site/toggle-vnj",
        "site/toggle-white-card",
        "site/toggle-featured",
        "site/meta",
    ],
)
def test_site_routes_without_csrf_are_403(client, db, route):
    r = client.post(f"/match/1/{route}", headers=AUTH)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_toggle_public_creates_then_flips(client, db):
    match_id = await _seed_match(db)
    # absent → create with is_public True
    r = client.post(f"/match/{match_id}/site/toggle-public", headers={**AUTH, **CSRF})
    assert r.status_code == 200
    sl = await _site_listing(db, match_id)
    assert sl is not None and sl.is_public is True
    # present → flip to False
    client.post(f"/match/{match_id}/site/toggle-public", headers={**AUTH, **CSRF})
    sl = await _site_listing(db, match_id)
    assert sl.is_public is False
    assert await _site_listing_count(db) == 1


@pytest.mark.asyncio
async def test_toggle_vnj_creates_and_flips(client, db):
    match_id = await _seed_match(db)
    client.post(f"/match/{match_id}/site/toggle-vnj", headers={**AUTH, **CSRF})
    sl = await _site_listing(db, match_id)
    assert sl is not None and sl.is_for_vnj is True
    client.post(f"/match/{match_id}/site/toggle-vnj", headers={**AUTH, **CSRF})
    assert (await _site_listing(db, match_id)).is_for_vnj is False


@pytest.mark.asyncio
async def test_toggle_white_card_creates_and_flips(client, db):
    match_id = await _seed_match(db)
    client.post(f"/match/{match_id}/site/toggle-white-card", headers={**AUTH, **CSRF})
    sl = await _site_listing(db, match_id)
    assert sl is not None and sl.is_for_white_card is True
    client.post(f"/match/{match_id}/site/toggle-white-card", headers={**AUTH, **CSRF})
    assert (await _site_listing(db, match_id)).is_for_white_card is False


@pytest.mark.asyncio
async def test_toggle_featured_creates_then_flips(client, db):
    match_id = await _seed_match(db)
    # absent → create with is_featured True (idempotent upsert)
    r = client.post(f"/match/{match_id}/site/toggle-featured", headers={**AUTH, **CSRF})
    assert r.status_code == 200
    sl = await _site_listing(db, match_id)
    assert sl is not None and sl.is_featured is True
    # present → flip back to False
    client.post(f"/match/{match_id}/site/toggle-featured", headers={**AUTH, **CSRF})
    sl = await _site_listing(db, match_id)
    assert sl.is_featured is False
    assert await _site_listing_count(db) == 1  # upsert, not a second row


@pytest.mark.asyncio
async def test_toggle_featured_creating_row_keeps_other_defaults(client, db):
    """Toggling featured on a fresh match sets only is_featured; is_public default True, flags False."""
    match_id = await _seed_match(db, raw_text="под ВНЖ и бели картон")
    client.post(f"/match/{match_id}/site/toggle-featured", headers={**AUTH, **CSRF})
    sl = await _site_listing(db, match_id)
    assert sl.is_featured is True
    assert sl.is_public is True
    # creating via a toggle does not run the text-parse
    assert sl.is_for_vnj is False
    assert sl.is_for_white_card is False


@pytest.mark.asyncio
async def test_toggle_featured_preserves_existing_flags(client, db):
    match_id = await _seed_match(db)
    async with db() as s:
        s.add(SiteListing(match_id=match_id, is_public=False, is_for_vnj=True, note="ручная"))
        await s.commit()
    client.post(f"/match/{match_id}/site/toggle-featured", headers={**AUTH, **CSRF})
    sl = await _site_listing(db, match_id)
    assert sl.is_featured is True
    # toggling featured must not clobber other curated fields
    assert sl.is_public is False
    assert sl.is_for_vnj is True
    assert sl.note == "ручная"


@pytest.mark.asyncio
async def test_toggle_creating_row_does_not_default_other_flags(client, db):
    """Toggling vnj on a fresh match creates a row with only that flag set; is_public default True."""
    match_id = await _seed_match(db, raw_text="под ВНЖ и бели картон")
    client.post(f"/match/{match_id}/site/toggle-vnj", headers={**AUTH, **CSRF})
    sl = await _site_listing(db, match_id)
    assert sl.is_for_vnj is True
    # creating via a toggle does not run the text-parse; white-card stays default False
    assert sl.is_for_white_card is False
    assert sl.is_public is True


@pytest.mark.asyncio
async def test_meta_sets_channel_url_and_note(client, db):
    match_id = await _seed_match(db)
    r = client.post(
        f"/match/{match_id}/site/meta",
        headers={**AUTH, **CSRF},
        data={"channel_post_url": "https://t.me/RentBelgrad/123", "note": "торг уместен"},
    )
    assert r.status_code == 200
    sl = await _site_listing(db, match_id)
    assert sl is not None
    assert sl.channel_post_url == "https://t.me/RentBelgrad/123"
    assert sl.note == "торг уместен"


@pytest.mark.asyncio
async def test_meta_preserves_existing_flags(client, db):
    match_id = await _seed_match(db)
    async with db() as s:
        s.add(SiteListing(match_id=match_id, is_public=False, is_for_vnj=True))
        await s.commit()
    client.post(
        f"/match/{match_id}/site/meta",
        headers={**AUTH, **CSRF},
        data={"channel_post_url": "https://t.me/RentBelgrad/9", "note": ""},
    )
    sl = await _site_listing(db, match_id)
    assert sl.is_public is False
    assert sl.is_for_vnj is True
    assert sl.channel_post_url == "https://t.me/RentBelgrad/9"


@pytest.mark.asyncio
async def test_site_routes_on_missing_match_404(client, db):
    r = client.post("/match/999/site/toggle-public", headers={**AUTH, **CSRF})
    assert r.status_code == 404


# --------------------------------------------------------------------------- queue card surfacing


@pytest.mark.asyncio
async def test_queue_card_shows_site_toggles(client, db):
    await _seed_match(db)
    r = client.get("/queue", headers=AUTH)
    assert r.status_code == 200
    assert "/site/toggle-public" in r.text
    assert "/site/toggle-vnj" in r.text
    assert "/site/toggle-white-card" in r.text
    assert "/site/toggle-featured" in r.text
    assert "На сайт" in r.text
    assert "Квартира недели" in r.text


# --------------------------------------------------------------------------- W2 follow-up: exclusivity + /published controls


@pytest.mark.asyncio
async def test_toggle_featured_is_exclusive(client, db):
    """«Квартира недели» is exclusive: featuring one clears any other featured listing."""
    async with db() as s:
        ch = SourceChannel(tg_id=-1002, username="srcchan2", is_active=True)
        s.add(ch)
        await s.flush()
        ids = []
        for i in (1, 2):
            lst = Listing(
                platform=Platform.NEKRETNINE, external_id=f"feat{i}", url=f"https://x/{i}",
                price_eur=800, photo_urls=["u"], photo_phashes=["00"],
            )
            ib = InboundPost(
                source_channel_id=ch.id, tg_message_id=100 + i, raw_text="",
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
            ids.append(m.id)
        await s.commit()
    a, b = ids

    client.post(f"/match/{a}/site/toggle-featured", headers={**AUTH, **CSRF})
    assert (await _site_listing(db, a)).is_featured is True

    # featuring B must demote A — at most one featured at a time
    client.post(f"/match/{b}/site/toggle-featured", headers={**AUTH, **CSRF})
    assert (await _site_listing(db, b)).is_featured is True
    assert (await _site_listing(db, a)).is_featured is False


@pytest.mark.asyncio
async def test_published_page_shows_controls_and_status(client, db):
    """/published renders the per-flat site controls, the live badge, and the window filter."""
    from src.db.models import PublishedPost

    match_id = await _seed_match(db, status=MatchStatus.PUBLISHED)
    client.post(f"/match/{match_id}/site/toggle-public", headers={**AUTH, **CSRF})  # put it on the site
    async with db() as s:
        s.add(PublishedPost(match_id=match_id, tg_message_id=999, post_text="x"))
        await s.commit()

    r = client.get("/published", headers=AUTH)
    assert r.status_code == 200
    html = r.text
    assert "На сайт" in html and "Квартира недели" in html       # controls present
    assert "за неделю" in html and "за 3 недели" in html         # window filter
    assert "на сайте" in html                                    # live badge (we toggled public)
