"""Smoke tests for the admin endpoints — basic auth, page rendering, photo proxy."""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
from src.admin.app import create_app
from src.db.models import (
    Base,
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SourceChannel,
)

CREDS = base64.b64encode(b"darya:test_secret_123").decode()
AUTH = {"Authorization": f"Basic {CREDS}"}


@pytest.fixture(autouse=True)
def _force_admin_password(monkeypatch):
    """Tests use a fixed username/password regardless of .env so they stay deterministic."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "admin_username", "darya")
    monkeypatch.setattr(s, "admin_password", "test_secret_123")
    yield


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    orig_engine = db_session._engine
    orig_factory = db_session._session_factory
    db_session._engine = engine
    db_session._session_factory = factory
    try:
        yield factory
    finally:
        await engine.dispose()
        db_session._engine = orig_engine
        db_session._session_factory = orig_factory


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


def test_requires_auth(client):
    r = client.get("/queue")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("basic")


def test_wrong_password_is_unauthorized(client):
    bad = base64.b64encode(b"darya:wrong").decode()
    r = client.get("/queue", headers={"Authorization": f"Basic {bad}"})
    assert r.status_code == 401


def test_default_password_rejected_even_when_correct(client, monkeypatch):
    """Fail closed: the shipped default 'change_me' must never grant access."""
    from src.config import get_settings
    monkeypatch.setattr(get_settings(), "admin_password", "change_me")
    creds = base64.b64encode(b"darya:change_me").decode()
    r = client.get("/queue", headers={"Authorization": f"Basic {creds}"})
    assert r.status_code == 401


def test_non_ascii_configured_password_returns_401_not_500(client, monkeypatch):
    """compare_digest would TypeError (→ 500) on non-ASCII str; must be a clean 401."""
    from src.config import get_settings
    monkeypatch.setattr(get_settings(), "admin_password", "пароль")
    bad = base64.b64encode(b"darya:wrong").decode()
    r = client.get("/queue", headers={"Authorization": f"Basic {bad}"})
    assert r.status_code == 401


def test_empty_queue_renders(client, db):
    r = client.get("/queue", headers=AUTH)
    assert r.status_code == 200
    assert "Очередь" in r.text
    assert "Пусто" in r.text


def test_root_redirects_to_queue(client):
    r = client.get("/", headers=AUTH, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/queue"


@pytest.mark.asyncio
async def test_queue_renders_pending_match(client, db):
    async with db() as s:
        ch = SourceChannel(tg_id=-1001, username="srcchan", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id="ext1",
            url="https://nekretnine.rs/x",
            price_eur=850,
            area_m2=65,
            rooms=2.5,
            district="Vračar",
            photo_urls=["u1", "u2"],
            photo_phashes=["00", "11"],
        )
        ib = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=42,
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
            matched_photo_count=2,
            method="phash",
            status=MatchStatus.PENDING,
        )
        s.add(m)
        await s.commit()

    r = client.get("/queue", headers=AUTH)
    assert r.status_code == 200
    assert "Цена: 850 евро" in r.text
    assert "#Vracar" in r.text
    # textarea must be pre-populated with the rendered post text
    assert "@Darya_kralj" in r.text
    # copy + reject buttons present
    assert "Скопировать пост" in r.text
    assert 'hx-post="/match/' in r.text and "/reject" in r.text
    # No bot-API publish action anywhere (the /published page in nav is the delivery log)
    assert "/match/" not in r.text or "/publish" not in r.text.replace("/published", "")
    assert 'hx-post="/match/' in r.text  # still has reject hook
    assert "/publish\"" not in r.text  # but no /publish endpoint
    assert "Опубликовать" not in r.text


def test_sources_page_empty(client, db):
    r = client.get("/sources", headers=AUTH)
    assert r.status_code == 200
    assert "otz add-source" in r.text


def test_listings_page_empty(client, db):
    r = client.get("/listings", headers=AUTH)
    assert r.status_code == 200
    assert "Listings" in r.text
