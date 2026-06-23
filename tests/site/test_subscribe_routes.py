"""C2 — POST /subscribe + GET /unsubscribe routes (honeypot/CSRF/rate-limit mirror leads)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from jinja2 import DictLoader
from sqlalchemy import select

import src.site.routes as routes
from src.db.models import Subscription
from src.site import cache, db as sitedb
from src.site.app import create_app
from src.site.deps import CSRF_FORM_FIELD, SITE_CSRF_TOKEN, catalog_db_url, lead_rate_limiter

_STUBS = {
    "base.html": "BASE",
    # /catalog is the email-channel redirect target; a trivial stub avoids loading D's real markup.
    "catalog.html": "TPL:catalog",
    "partials/_results.html": "TPL:_results",
    "partials/_filters.html": "TPL:_filters",
    "partials/_subscribe_ok.html": "TPL:_subscribe_ok link={{ telegram_deep_link or 'none' }}",
    "unsubscribe.html": "TPL:unsubscribe ok={{ ok }}",
}


@pytest.fixture(autouse=True)
def _stub_templates(monkeypatch):
    monkeypatch.setattr(routes.templates.env, "loader", DictLoader(_STUBS))
    yield


@pytest.fixture(autouse=True)
def _clear_state():
    cache.catalog_cache.clear()
    lead_rate_limiter.reset()
    yield
    lead_rate_limiter.reset()


@pytest.fixture
def client(sub_db_url):
    app = create_app()
    app.dependency_overrides[catalog_db_url] = lambda: sub_db_url
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sub_db_url(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.db.models import Base

    db_path = tmp_path / "sub_routes.db"
    url = f"sqlite+aiosqlite:///{db_path}"

    async def _make():
        engine = create_async_engine(url, future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_make())
    return url


def _form(**extra) -> dict[str, str]:
    # A subscription needs at least one criterion (empty criteria is rejected), so default to a
    # price cap; callers can override with price_max="" to exercise the empty-criteria path.
    return {CSRF_FORM_FIELD: SITE_CSRF_TOKEN, "price_max": "800", **extra}


def _fetch_subs(url: str) -> list[dict]:
    async def _go():
        async with sitedb.read_session(database_url=url) as s:
            rows = (await s.execute(select(Subscription).order_by(Subscription.id))).scalars().all()
            return [
                {"id": r.id, "channel": r.channel, "email": r.email, "is_active": r.is_active,
                 "token": r.token, "criteria": r.criteria}
                for r in rows
            ]

    return asyncio.run(_go())


# ------------------------------------------------------------------ POST /subscribe


def test_subscribe_email_writes_active_row(client):
    r = client.post(
        "/subscribe",
        data=_form(channel="email", email="a@b.com", district="Врачар", price_max="800"),
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    subs = _fetch_subs(client.app.dependency_overrides[catalog_db_url]())
    assert len(subs) == 1
    assert subs[0]["channel"] == "email"
    assert subs[0]["email"] == "a@b.com"
    assert subs[0]["is_active"] is True
    assert subs[0]["criteria"]["district"] == ["Врачар"]
    assert subs[0]["criteria"]["price_max"] == 800


def test_subscribe_without_csrf_is_403(client):
    r = client.post("/subscribe", data={"channel": "email", "email": "a@b.com"})
    assert r.status_code == 403


def test_subscribe_honeypot_writes_nothing(client):
    r = client.post(
        "/subscribe",
        data=_form(channel="email", email="bot@x.com", company="ACME"),
    )
    assert r.status_code in (200, 303)  # silent OK
    assert _fetch_subs(client.app.dependency_overrides[catalog_db_url]()) == []


def test_subscribe_invalid_email_is_400(client):
    r = client.post("/subscribe", data=_form(channel="email", email="not-an-email"))
    assert r.status_code == 400


def test_subscribe_rate_limited_after_max(client, monkeypatch):
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "site_lead_ratelimit_max", 2)
    monkeypatch.setattr(settings, "site_lead_ratelimit_window", 3600)
    ok1 = client.post("/subscribe", data=_form(channel="email", email="a@b.com"), follow_redirects=False)
    ok2 = client.post("/subscribe", data=_form(channel="email", email="b@b.com"), follow_redirects=False)
    blocked = client.post("/subscribe", data=_form(channel="email", email="c@b.com"), follow_redirects=False)
    assert ok1.status_code in (200, 303)
    assert ok2.status_code in (200, 303)
    assert blocked.status_code == 429


def test_subscribe_telegram_returns_deep_link(client, monkeypatch):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "site_telegram_bot_username", "RentBelgradBot")
    r = client.post(
        "/subscribe",
        data=_form(channel="telegram", district="Земун"),
        headers={"hx-request": "true"},
    )
    assert r.status_code == 200
    subs = _fetch_subs(client.app.dependency_overrides[catalog_db_url]())
    assert len(subs) == 1
    assert subs[0]["channel"] == "telegram"
    assert subs[0]["is_active"] is False  # not linked yet
    # The HTMX partial echoes the deep-link carrying the token.
    assert f"start=sub_{subs[0]['token']}" in r.text or "RentBelgradBot" in r.text


# ------------------------------------------------------------------ GET /unsubscribe


def test_unsubscribe_flips_is_active(client):
    client.post("/subscribe", data=_form(channel="email", email="a@b.com"))
    token = _fetch_subs(client.app.dependency_overrides[catalog_db_url]())[0]["token"]

    r = client.get(f"/unsubscribe?token={token}")
    assert r.status_code == 200
    assert "ok=True" in r.text
    assert _fetch_subs(client.app.dependency_overrides[catalog_db_url]())[0]["is_active"] is False


def test_unsubscribe_unknown_token_renders_gracefully(client):
    r = client.get("/unsubscribe?token=does-not-exist")
    assert r.status_code == 200
    assert "ok=False" in r.text
