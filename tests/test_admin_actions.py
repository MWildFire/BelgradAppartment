"""Admin write actions (W3-F1/F2/F3) — CSRF enforcement, reject / mark-posted idempotency, /metrics."""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.admin.app import create_app
from src.admin.csrf import CSRF_TOKEN
from src.db.models import (
    DailyMetrics,
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    PublishedPost,
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
    # conftest's `db` yields the engine; admin helpers need the session factory —
    # reuse the shared fresh_db plumbing instead of duplicating the engine/swap dance.
    async with fresh_db() as (_engine, factory):
        yield factory


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


async def _seed_match(db, status: MatchStatus = MatchStatus.PENDING) -> int:
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


async def _match_state(db, match_id: int) -> tuple[MatchStatus, str | None]:
    async with db() as s:
        m = await s.get(Match, match_id)
        return m.status, m.rejection_reason


async def _published_count(db) -> int:
    async with db() as s:
        return (await s.execute(select(func.count()).select_from(PublishedPost))).scalar_one()


# --------------------------------------------------------------------------- CSRF (W3-F2)


@pytest.mark.asyncio
async def test_post_without_csrf_is_403_and_no_effect(client, db):
    match_id = await _seed_match(db)
    r = client.post(f"/match/{match_id}/reject", headers=AUTH, data={"reason": "x"})
    assert r.status_code == 403
    status, _ = await _match_state(db, match_id)
    assert status == MatchStatus.PENDING


@pytest.mark.asyncio
async def test_post_with_wrong_csrf_is_403(client, db):
    match_id = await _seed_match(db)
    r = client.post(
        f"/match/{match_id}/mark-posted",
        headers={**AUTH, "X-CSRF-Token": "not-the-token"},
    )
    assert r.status_code == 403
    status, _ = await _match_state(db, match_id)
    assert status == MatchStatus.PENDING


@pytest.mark.asyncio
async def test_csrf_accepted_via_form_field(client, db):
    match_id = await _seed_match(db)
    r = client.post(
        f"/match/{match_id}/mark-posted", headers=AUTH, data={"csrf_token": CSRF_TOKEN}
    )
    assert r.status_code == 200
    status, _ = await _match_state(db, match_id)
    assert status == MatchStatus.PUBLISHED


def test_sources_toggle_without_csrf_is_403(client, db):
    r = client.post("/sources/1/toggle", headers=AUTH)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_sources_toggle_with_csrf_flips(client, db):
    async with db() as s:
        s.add(SourceChannel(tg_id=-1002, username="chan2", is_active=True))
        await s.commit()
    r = client.post(
        "/sources/1/toggle",
        headers=AUTH,
        data={"csrf_token": CSRF_TOKEN},
        follow_redirects=False,
    )
    assert r.status_code == 303
    async with db() as s:
        row = await s.get(SourceChannel, 1)
        assert row.is_active is False


def test_post_without_auth_is_401_even_with_csrf(client, db):
    # auth is checked before CSRF — unauthenticated callers get the basic-auth challenge
    r = client.post("/match/1/reject", headers=CSRF, data={"reason": ""})
    assert r.status_code == 401


def test_pages_render_csrf_token(client, db):
    r = client.get("/queue", headers=AUTH)
    assert r.status_code == 200
    assert CSRF_TOKEN in r.text  # hx-headers on <body>


# --------------------------------------------------------------------------- Actions (W3-F1)


@pytest.mark.asyncio
async def test_reject_sets_status_and_reason(client, db):
    match_id = await _seed_match(db)
    r = client.post(
        f"/match/{match_id}/reject", headers={**AUTH, **CSRF}, data={"reason": "дубль"}
    )
    assert r.status_code == 200
    assert "отклонено" in r.text
    status, reason = await _match_state(db, match_id)
    assert status == MatchStatus.REJECTED
    assert reason == "дубль"


@pytest.mark.asyncio
async def test_reject_with_form_token_and_reason_in_same_body(client, db):
    """require_csrf reads request.form() before FastAPI parses Form(reason) —
    Starlette must serve both from its body cache."""
    match_id = await _seed_match(db)
    r = client.post(
        f"/match/{match_id}/reject",
        headers=AUTH,
        data={"csrf_token": CSRF_TOKEN, "reason": "дубль"},
    )
    assert r.status_code == 200
    status, reason = await _match_state(db, match_id)
    assert status == MatchStatus.REJECTED
    assert reason == "дубль"


@pytest.mark.asyncio
async def test_reject_is_idempotent_and_keeps_reason(client, db):
    match_id = await _seed_match(db)
    r1 = client.post(
        f"/match/{match_id}/reject", headers={**AUTH, **CSRF}, data={"reason": "дубль"}
    )
    # re-POST with empty reason (stale page / double click) — no error, reason preserved
    r2 = client.post(f"/match/{match_id}/reject", headers={**AUTH, **CSRF}, data={"reason": ""})
    assert r1.status_code == r2.status_code == 200
    status, reason = await _match_state(db, match_id)
    assert status == MatchStatus.REJECTED
    assert reason == "дубль"


@pytest.mark.asyncio
async def test_mark_posted_sets_status_no_delivery_row(client, db):
    match_id = await _seed_match(db)
    r = client.post(f"/match/{match_id}/mark-posted", headers={**AUTH, **CSRF})
    assert r.status_code == 200
    assert "размещено вручную" in r.text
    status, _ = await _match_state(db, match_id)
    assert status == MatchStatus.PUBLISHED
    # status markup only: NOTHING was sent, no PublishedPost row appears
    assert await _published_count(db) == 0


@pytest.mark.asyncio
async def test_mark_posted_is_idempotent(client, db):
    match_id = await _seed_match(db)
    r1 = client.post(f"/match/{match_id}/mark-posted", headers={**AUTH, **CSRF})
    r2 = client.post(f"/match/{match_id}/mark-posted", headers={**AUTH, **CSRF})
    assert r1.status_code == r2.status_code == 200
    status, _ = await _match_state(db, match_id)
    assert status == MatchStatus.PUBLISHED
    assert await _published_count(db) == 0


@pytest.mark.asyncio
async def test_actions_on_missing_match_return_error_card(client, db):
    r = client.post("/match/999/reject", headers={**AUTH, **CSRF}, data={"reason": ""})
    assert r.status_code == 400
    assert "не найден" in r.text
    r = client.post("/match/999/mark-posted", headers={**AUTH, **CSRF})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_rejected_match_leaves_pending_queue(client, db):
    match_id = await _seed_match(db)
    client.post(f"/match/{match_id}/reject", headers={**AUTH, **CSRF}, data={"reason": ""})
    r = client.get("/queue", headers=AUTH)
    assert r.status_code == 200
    assert "Пусто" in r.text


@pytest.mark.asyncio
async def test_queue_card_has_both_action_buttons(client, db):
    match_id = await _seed_match(db)
    r = client.get("/queue", headers=AUTH)
    assert r.status_code == 200
    assert f'hx-post="/match/{match_id}/reject"' in r.text
    assert f'hx-post="/match/{match_id}/mark-posted"' in r.text
    # both have a confirm step
    assert r.text.count("hx-confirm") >= 2


# --------------------------------------------------------------------------- /metrics (W3-F3)


def test_metrics_requires_auth(client, db):
    assert client.get("/metrics").status_code == 401


def test_metrics_empty_renders(client, db):
    r = client.get("/metrics", headers=AUTH)
    assert r.status_code == 200
    assert "Метрик пока нет" in r.text


@pytest.mark.asyncio
async def test_metrics_with_data(client, db):
    async with db() as s:
        s.add_all([
            DailyMetrics(snapshot_date="2026-06-10", platform="nekretnine", active_count=1200, no_photo_count=30),
            DailyMetrics(snapshot_date="2026-06-10", platform="cetiri_zida", active_count=900, no_photo_count=12),
            DailyMetrics(snapshot_date="2026-06-11", platform="nekretnine", active_count=1180, no_photo_count=28),
        ])
        await s.commit()
    r = client.get("/metrics", headers=AUTH)
    assert r.status_code == 200
    # table rows incl. no_photo_count
    assert "nekretnine" in r.text and "cetiri_zida" in r.text
    assert "1180" in r.text and "28" in r.text
    # chart payload: labels + per-platform datasets, missing day → null (tojson sorts keys)
    assert '"labels": ["2026-06-10", "2026-06-11"]' in r.text
    assert '"data": [900, null], "label": "cetiri_zida"' in r.text
