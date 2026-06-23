"""F1 — leads funnel + district/budget demand aggregation (pure functions + admin block).

The aggregation helpers are pure over a list of Lead rows so the bulk of the logic is
unit-tested without HTTP. A final smoke test asserts the /leads page renders the block.
"""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from src.admin.app import create_app
from src.admin.leads_report import (
    BUDGET_BANDS,
    budget_demand,
    build_leads_report,
    district_demand,
    format_leads_report,
    leads_funnel,
)
from src.db.models import Lead
from tests.conftest import fresh_db

CREDS = base64.b64encode(b"darya:test_secret_123").decode()
AUTH = {"Authorization": f"Basic {CREDS}"}


@pytest.fixture(autouse=True)
def _force_admin_password(monkeypatch):
    from src.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "admin_username", "darya")
    monkeypatch.setattr(s, "admin_password", "test_secret_123")
    yield


def _lead(**kw) -> Lead:
    """Build an in-memory (unpersisted) Lead — enough for the pure aggregators."""
    kw.setdefault("name", "x")
    kw.setdefault("contact", "x")
    return Lead(**kw)


# --------------------------------------------------------------------------- funnel


def test_funnel_counts_and_conversion_rates():
    leads = [
        _lead(status="new"),
        _lead(status="new"),
        _lead(status="new"),
        _lead(status="new"),
        _lead(status="contacted"),
        _lead(status="contacted"),
        _lead(status="closed"),
    ]
    # totals seen at each stage are cumulative: every contacted/closed lead was once new,
    # every closed lead was once contacted.
    f = leads_funnel(leads)
    assert f["new"] == 4
    assert f["contacted"] == 2
    assert f["closed"] == 1
    assert f["total"] == 7
    # contacted-or-better / total ; closed / (contacted-or-better) ; closed / total
    assert f["rate_contacted"] == pytest.approx(3 / 7)  # 2 contacted + 1 closed
    assert f["rate_closed_of_contacted"] == pytest.approx(1 / 3)  # 1 closed of 3 reached contact
    assert f["rate_closed"] == pytest.approx(1 / 7)


def test_funnel_zero_leads_no_crash_zero_rates():
    f = leads_funnel([])
    assert f["new"] == 0
    assert f["contacted"] == 0
    assert f["closed"] == 0
    assert f["total"] == 0
    assert f["rate_contacted"] == 0
    assert f["rate_closed_of_contacted"] == 0
    assert f["rate_closed"] == 0


def test_funnel_ignores_unknown_status():
    leads = [_lead(status="new"), _lead(status="weird")]
    f = leads_funnel(leads)
    assert f["new"] == 1
    # unknown status still counts toward total but not toward a known stage
    assert f["total"] == 2
    assert f["contacted"] == 0


# --------------------------------------------------------------------------- districts


def test_district_demand_explodes_multivalue_and_orders_topn():
    leads = [
        _lead(districts="Vračar, Vozdovac"),
        _lead(districts="Vračar"),
        _lead(districts="Vračar; Savski venac"),
        _lead(districts="Vozdovac"),
        _lead(districts=None),
        _lead(districts="   "),
    ]
    rows = district_demand(leads, top=2)
    # Vračar=3, Vozdovac=2, Savski venac=1 → top-2 ordered by count desc
    assert rows == [("Vračar", 3), ("Vozdovac", 2)]


def test_district_demand_handles_slash_and_whitespace():
    leads = [_lead(districts="Novi Beograd / Zemun"), _lead(districts=" Zemun ")]
    rows = district_demand(leads, top=10)
    assert ("Zemun", 2) in rows
    assert ("Novi Beograd", 1) in rows


def test_district_demand_empty():
    assert district_demand([], top=5) == []


# --------------------------------------------------------------------------- budget


def test_budget_demand_bands_including_null():
    leads = [
        _lead(budget_eur=350),   # <400
        _lead(budget_eur=400),   # 400–600 (lower bound inclusive)
        _lead(budget_eur=550),   # 400–600
        _lead(budget_eur=700),   # 600–800
        _lead(budget_eur=950),   # 800–1000
        _lead(budget_eur=1500),  # 1000+
        _lead(budget_eur=None),  # не указан
        _lead(budget_eur=None),  # не указан
    ]
    bands = dict(budget_demand(leads))
    assert bands["< 400"] == 1
    assert bands["400–600"] == 2
    assert bands["600–800"] == 1
    assert bands["800–1000"] == 1
    assert bands["1000+"] == 1
    assert bands["не указан"] == 2


def test_budget_demand_preserves_band_order_and_full_coverage():
    rows = budget_demand([])
    labels = [label for label, _ in rows]
    # every defined band plus the null bucket is present, in canonical order, even with no leads
    assert labels == [b.label for b in BUDGET_BANDS] + ["не указан"]
    assert all(count == 0 for _, count in rows)


def test_budget_boundary_600_goes_up_not_into_lower_band():
    # boundary value lands in the band whose lower bound it equals (lower-inclusive)
    bands = dict(budget_demand([_lead(budget_eur=600), _lead(budget_eur=800), _lead(budget_eur=1000)]))
    assert bands["600–800"] == 1
    assert bands["800–1000"] == 1
    assert bands["1000+"] == 1
    assert bands["400–600"] == 0


# --------------------------------------------------------------------------- CLI formatting


def test_format_leads_report_renders_all_sections():
    leads = [
        _lead(status="new", districts="Vračar", budget_eur=900),
        _lead(status="contacted", districts="Vračar, Zemun", budget_eur=550),
        _lead(status="closed", districts="Zemun", budget_eur=None),
    ]
    out = format_leads_report(build_leads_report(leads))
    # all three sections, an exploded district, a budget band, and the null bucket appear
    assert "Воронка" in out
    assert "Районы" in out or "район" in out.lower()
    assert "Бюджет" in out or "бюджет" in out.lower()
    assert "Vračar" in out
    assert "400–600" in out
    assert "не указан" in out


def test_format_leads_report_empty_no_crash():
    out = format_leads_report(build_leads_report([]))
    assert "Воронка" in out
    # zero-lead funnel renders a 0 total without raising
    assert "0" in out


# --------------------------------------------------------------------------- admin block smoke


@pytest_asyncio.fixture
async def db():
    async with fresh_db() as (_engine, factory):
        yield factory


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


async def _seed(db, **kw):
    async with db() as s:
        s.add(Lead(name=kw.pop("name", "x"), contact=kw.pop("contact", "x"), **kw))
        await s.commit()


@pytest.mark.asyncio
async def test_leads_page_renders_demand_block(client, db):
    await _seed(db, status="new", districts="Vračar", budget_eur=900)
    await _seed(db, status="contacted", districts="Vračar, Zemun", budget_eur=550)
    await _seed(db, status="closed", districts="Zemun", budget_eur=None)
    r = client.get("/leads", headers=AUTH)
    assert r.status_code == 200
    # funnel + demand section headers + an exploded district + a budget band label all render
    assert "Воронка" in r.text
    assert "Спрос по районам" in r.text
    assert "Vračar" in r.text
    assert "400–600" in r.text
    assert "не указан" in r.text


def test_leads_page_demand_block_empty_no_crash(client, db):
    r = client.get("/leads", headers=AUTH)
    assert r.status_code == 200
    # the block still renders its headers with zero leads (no division-by-zero 500)
    assert "Воронка" in r.text
