"""Admin leads inbox (W4-1) — GET list (newest-first, filter) + CSRF-guarded status POST."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from src.admin.app import create_app
from src.admin.csrf import CSRF_TOKEN
from src.db.models import Lead, SiteListing
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


async def _seed_lead(db, **kw) -> int:
    async with db() as s:
        lead = Lead(
            name=kw.pop("name", "Иван"),
            contact=kw.pop("contact", "@ivan"),
            **kw,
        )
        s.add(lead)
        await s.commit()
        return lead.id


async def _lead_status(db, lead_id: int) -> str:
    async with db() as s:
        return (await s.get(Lead, lead_id)).status


# --------------------------------------------------------------------------- GET /leads


def test_leads_requires_auth(client, db):
    assert client.get("/leads").status_code == 401


def test_leads_empty_renders(client, db):
    r = client.get("/leads", headers=AUTH)
    assert r.status_code == 200
    assert "Заявки" in r.text


@pytest.mark.asyncio
async def test_leads_lists_seeded(client, db):
    await _seed_lead(db, name="Анна", contact="anna@x.io", budget_eur=900,
                     districts="Vračar", rooms="2", message="ищу тихую квартиру")
    r = client.get("/leads", headers=AUTH)
    assert r.status_code == 200
    assert "Анна" in r.text
    assert "anna@x.io" in r.text
    assert "900" in r.text
    assert "Vračar" in r.text
    assert "ищу тихую квартиру" in r.text


@pytest.mark.asyncio
async def test_leads_newest_first(client, db):
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = old + timedelta(days=10)
    await _seed_lead(db, name="Старый", created_at=old)
    await _seed_lead(db, name="Новый", created_at=new)
    r = client.get("/leads", headers=AUTH)
    assert r.status_code == 200
    # newest lead must appear before the older one in the rendered list
    assert r.text.index("Новый") < r.text.index("Старый")


@pytest.mark.asyncio
async def test_listing_lead_shows_apartment_reference(client, db):
    async with db() as s:
        site = SiteListing(match_id=1)
        s.add(site)
        await s.commit()
        site_id = site.id
    await _seed_lead(db, name="Пётр", kind="listing", site_listing_id=site_id)
    r = client.get("/leads", headers=AUTH)
    assert r.status_code == 200
    # apartment reference rendered as «квартира #<id>» linking to public /apt/<id>
    assert f"#{site_id}" in r.text
    assert f"/apt/{site_id}" in r.text


@pytest.mark.asyncio
async def test_status_filter_narrows_list(client, db):
    await _seed_lead(db, name="НовыйЛид", status="new")
    await _seed_lead(db, name="ЗакрытыйЛид", status="closed")
    r = client.get("/leads?status=new", headers=AUTH)
    assert r.status_code == 200
    assert "НовыйЛид" in r.text
    assert "ЗакрытыйЛид" not in r.text


# --------------------------------------------------------------------------- POST status


@pytest.mark.asyncio
async def test_status_change_requires_csrf(client, db):
    lead_id = await _seed_lead(db, status="new")
    r = client.post(f"/leads/{lead_id}/status", headers=AUTH, data={"status": "contacted"})
    assert r.status_code == 403
    assert await _lead_status(db, lead_id) == "new"


@pytest.mark.asyncio
async def test_status_change_with_csrf(client, db):
    lead_id = await _seed_lead(db, status="new")
    r = client.post(
        f"/leads/{lead_id}/status",
        headers=AUTH,
        data={"csrf_token": CSRF_TOKEN, "status": "contacted"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    assert await _lead_status(db, lead_id) == "contacted"


@pytest.mark.asyncio
async def test_status_change_invalid_value_rejected(client, db):
    lead_id = await _seed_lead(db, status="new")
    r = client.post(
        f"/leads/{lead_id}/status",
        headers={**AUTH, **CSRF},
        data={"status": "garbage"},
    )
    assert r.status_code == 400
    # invalid value must not mutate the row
    assert await _lead_status(db, lead_id) == "new"


@pytest.mark.asyncio
async def test_status_change_missing_lead_404(client, db):
    r = client.post(
        f"/leads/999/status",
        headers={**AUTH, **CSRF},
        data={"status": "closed"},
    )
    assert r.status_code == 404


def test_leads_page_renders_csrf_token(client, db):
    r = client.get("/leads", headers=AUTH)
    assert r.status_code == 200
    assert CSRF_TOKEN in r.text
