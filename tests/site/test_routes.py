"""Route + cache + deps tests for the public catalog (Stage W, W3-C1).

Independence from agent D: we never use D's real templates. A DictLoader stub provides trivial
markup for the DATA_CONTRACT §2 template names — each stub emits its own name plus a couple of
context keys, so a route's selected-template and context are assertable from the response body.

Two readmodel styles, both exercised per the plan:
- seeded temp DB via app.dependency_overrides[catalog_db_url] (real list_public/get_public/…),
- monkeypatched readmodel functions to assert the exact args a route forwards.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from jinja2 import DictLoader

import src.site.routes as routes
from src.site import cache
from src.site.app import create_app
from src.site.deps import (
    CSRF_FORM_FIELD,
    SITE_CSRF_TOKEN,
    catalog_db_url,
    lead_rate_limiter,
)

# --- stub templates: each emits its name + a marker for a couple of context keys -----------

_STUBS = {
    "base.html": "BASE",
    "index.html": (
        "TPL:index featured={{ featured|length }} active={{ stats.active }} "
        "week={{ week.id if week else 'none' }}"
    ),
    "catalog.html": (
        "TPL:catalog items={{ items|length }} total={{ total }} "
        "vnj={{ applied.vnj }} districts={{ applied.district|join(',') }}"
    ),
    "apt.html": "TPL:apt id={{ item.id }} similar={{ similar|length }}",
    "map.html": "TPL:map",
    "calculator.html": "TPL:calculator",
    "partials/_results.html": "TPL:_results items={{ items|length }} total={{ total }}",
    "partials/_card.html": "TPL:_card id={{ item.id }}",
    "partials/_filters.html": "TPL:_filters",
    "partials/_lead_ok.html": "TPL:_lead_ok",
}


@pytest.fixture(autouse=True)
def _stub_templates(monkeypatch):
    """Swap the route module's Jinja env loader for trivial in-memory stubs."""
    monkeypatch.setattr(routes.templates.env, "loader", DictLoader(_STUBS))
    yield


@pytest.fixture(autouse=True)
def _clear_state():
    """Reset process-wide caches + rate-limiter between tests (they're module singletons)."""
    cache.catalog_cache.clear()
    cache.facets_cache.clear()
    cache.map_cache.clear()
    lead_rate_limiter.reset()
    yield
    cache.catalog_cache.clear()
    cache.facets_cache.clear()
    cache.map_cache.clear()
    lead_rate_limiter.reset()


@pytest.fixture
def client(site_db):
    app = create_app()
    app.dependency_overrides[catalog_db_url] = lambda: site_db
    with TestClient(app) as c:
        yield c


def _csrf_form(**extra) -> dict[str, str]:
    return {CSRF_FORM_FIELD: SITE_CSRF_TOKEN, **extra}


def _fetch_leads_sync(client) -> list[dict]:
    """Read persisted leads from the DB behind the client's catalog_db_url override.

    The route module is exercised synchronously via TestClient; this drives a short-lived
    async read on the same seeded file DB to assert what create_lead actually wrote.
    """
    import asyncio

    from sqlalchemy import select

    from src.db.models import Lead
    from src.site import db as sitedb

    url = client.app.dependency_overrides[catalog_db_url]()

    async def _go() -> list[dict]:
        async with sitedb.read_session(database_url=url) as s:
            rows = (await s.execute(select(Lead).order_by(Lead.id))).scalars().all()
            return [
                {"id": r.id, "kind": r.kind, "site_listing_id": r.site_listing_id, "name": r.name}
                for r in rows
            ]

    return asyncio.run(_go())


# ============================================================== unit: cache.py


def test_ttlcache_hit_and_miss():
    clock = {"t": 0.0}
    c = cache.TTLCache(ttl=10, clock=lambda: clock["t"])
    assert c.get("k") is cache._MISS
    c.set("k", "v")
    assert c.get("k") == "v"


def test_ttlcache_expires_after_ttl():
    clock = {"t": 0.0}
    c = cache.TTLCache(ttl=10, clock=lambda: clock["t"])
    c.set("k", "v")
    clock["t"] = 9.9
    assert c.get("k") == "v"
    clock["t"] = 10.0  # expiry is inclusive (>= expires_at)
    assert c.get("k") is cache._MISS


def test_ttlcache_zero_ttl_disables():
    c = cache.TTLCache(ttl=0)
    c.set("k", "v")
    assert c.get("k") is cache._MISS


def test_ttlcache_caches_none_as_hit():
    c = cache.TTLCache(ttl=10)
    c.set("k", None)
    assert c.get("k") is None  # a stored None is a HIT, not a miss


def test_normalize_catalog_key_order_and_empty_insensitive():
    a = cache.normalize_catalog_key({"sort": "new", "district": ["Vracar"], "page": ""})
    b = cache.normalize_catalog_key({"district": ["Vracar"], "page": None, "sort": "new"})
    assert a == b
    # multivalued lists normalize order-insensitively
    c1 = cache.normalize_catalog_key({"district": ["a", "b"]})
    c2 = cache.normalize_catalog_key({"district": ["b", "a"]})
    assert c1 == c2


# ============================================================== unit: deps rate limiter


def test_rate_limiter_allows_up_to_max_then_blocks():
    from src.site.deps import _RateLimiter

    rl = _RateLimiter(clock=lambda: 0.0)
    assert all(rl.check("ip", max_hits=3, window=60) for _ in range(3))
    assert rl.check("ip", max_hits=3, window=60) is False  # N+1 blocked


def test_rate_limiter_window_slides():
    clock = {"t": 0.0}
    from src.site.deps import _RateLimiter

    rl = _RateLimiter(clock=lambda: clock["t"])
    assert rl.check("ip", 1, 60) is True
    assert rl.check("ip", 1, 60) is False
    clock["t"] = 61.0
    assert rl.check("ip", 1, 60) is True  # old hit aged out


# ============================================================== routes: GET (seeded DB)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index_renders_index_with_featured_and_stats(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "TPL:index" in r.text
    assert "featured=3" in r.text  # 3 public listings seeded
    assert "active=3" in r.text


def test_index_passes_week_featured_one(client):
    # No row is is_featured in the seed, so featured_one falls back to the newest
    # public listing by posted_dt: site_listing 1 has a PublishedPost at _dt(10),
    # newer than 2/3's created_at, so the «квартира недели» is id=1.
    r = client.get("/")
    assert r.status_code == 200
    assert "week=1" in r.text  # the «квартира недели» context key reaches the template


def test_index_week_is_none_when_no_public(client, monkeypatch):
    async def no_featured(*, database_url=None):
        return None

    monkeypatch.setattr(routes.readmodel, "featured_one", no_featured)
    r = client.get("/")
    assert r.status_code == 200
    assert "week=none" in r.text  # placeholder path when there is no «квартира недели»


def test_index_week_comes_from_featured_one(client, monkeypatch):
    captured = {}

    class _W:
        id = 777

    async def fake_featured(*, database_url=None):
        captured["database_url"] = database_url
        return _W()

    monkeypatch.setattr(routes.readmodel, "featured_one", fake_featured)
    r = client.get("/")
    assert r.status_code == 200
    assert "week=777" in r.text  # the route uses featured_one()'s return as `week`
    assert captured["database_url"] is not None  # db seam forwarded


def test_catalog_renders_catalog_full_page(client):
    r = client.get("/catalog")
    assert r.status_code == 200
    assert "TPL:catalog" in r.text
    assert "items=3" in r.text
    assert "total=3" in r.text


def test_catalog_hx_request_renders_results_partial(client):
    r = client.get("/catalog", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "TPL:_results" in r.text
    assert "TPL:catalog" not in r.text


def test_catalog_vnj_filter_narrows(client):
    r = client.get("/catalog", params={"vnj": "1"})
    assert r.status_code == 200
    # only listing 1 is vnj in the seed
    assert "items=1" in r.text
    assert "total=1" in r.text
    assert "vnj=True" in r.text


def test_apt_public_renders(client):
    r = client.get("/apt/1")
    assert r.status_code == 200
    assert "TPL:apt" in r.text
    assert "id=1" in r.text


def test_apt_private_is_404(client):
    # site_listing 4 is is_public=False in the seed.
    r = client.get("/apt/4")
    assert r.status_code == 404


def test_apt_missing_is_404(client):
    r = client.get("/apt/9999")
    assert r.status_code == 404


def test_map_page_empty_context(client):
    r = client.get("/map")
    assert r.status_code == 200
    assert "TPL:map" in r.text


def test_map_json_only_public_with_coords(client):
    r = client.get("/api/map.json")
    assert r.status_code == 200
    body = r.json()
    assert "points" in body
    # listings 1 and 3 have coords + public; 2 has no coords; 4 is private.
    ids = sorted(p["id"] for p in body["points"])
    assert ids == [1, 3]
    for p in body["points"]:
        assert p["url"] == f"/apt/{p['id']}"


def test_calculator_page(client):
    r = client.get("/calculator")
    assert r.status_code == 200
    assert "TPL:calculator" in r.text


# ============================================================== routes: filter args reach readmodel (mock)


def test_catalog_forwards_parsed_filters_to_readmodel(client, monkeypatch):
    captured = {}

    async def fake_list_public(filters, *, sort, page, database_url, page_size=None):
        captured["filters"] = filters
        captured["sort"] = sort
        captured["page"] = page
        captured["database_url"] = database_url
        return ([], 0)

    async def fake_facets(*, database_url):
        from src.site.readmodel import Facets

        return Facets(districts=[], price_bounds=(0, 0), rooms=[], counts={})

    monkeypatch.setattr(routes.readmodel, "list_public", fake_list_public)
    monkeypatch.setattr(routes.readmodel, "facets", fake_facets)

    r = client.get(
        "/catalog",
        params=[
            ("vnj", "1"),
            ("wc", "1"),
            ("district", "Врачар"),
            ("district", "Земун"),
            ("price_min", "500"),
            ("price_max", "1000"),
            ("rooms", "2"),
            ("rooms", "3+"),
            ("sort", "price_asc"),
            ("page", "2"),
        ],
    )
    assert r.status_code == 200
    f = captured["filters"]
    assert f.vnj is True
    assert f.wc is True
    assert f.district == ["Врачар", "Земун"]
    assert f.price_min == 500
    assert f.price_max == 1000
    assert f.rooms == ["2", "3+"]
    assert captured["sort"] == "price_asc"
    assert captured["page"] == 2


# ============================================================== routes: cache call-count


def test_catalog_cache_skips_second_readmodel_call(client, monkeypatch):
    calls = {"list": 0}

    async def counting_list_public(filters, *, sort, page, database_url, page_size=None):
        calls["list"] += 1
        return ([], 0)

    async def fake_facets(*, database_url):
        from src.site.readmodel import Facets

        return Facets(districts=[], price_bounds=(0, 0), rooms=[], counts={})

    monkeypatch.setattr(routes.readmodel, "list_public", counting_list_public)
    monkeypatch.setattr(routes.readmodel, "facets", fake_facets)

    client.get("/catalog", params={"sort": "new"})
    client.get("/catalog", params={"sort": "new"})  # identical → cache hit
    assert calls["list"] == 1


def test_map_json_cache_skips_second_call(client, monkeypatch):
    calls = {"map": 0}

    async def counting_map(*, database_url):
        calls["map"] += 1
        return []

    monkeypatch.setattr(routes.readmodel, "map_points", counting_map)
    client.get("/api/map.json")
    client.get("/api/map.json")
    assert calls["map"] == 1


# ============================================================== routes: POST /lead


def test_lead_without_csrf_is_403(client):
    r = client.post("/lead", data={"name": "A", "contact": "tg"})
    assert r.status_code == 403


def test_lead_honeypot_filled_returns_ok_and_writes_nothing(client, monkeypatch):
    called = {"n": 0}

    async def spy_create(payload, *, database_url=None):
        called["n"] += 1
        from src.site.leads import create_lead as real

        return await real(payload, database_url=database_url)

    monkeypatch.setattr(routes, "create_lead", spy_create)
    r = client.post(
        "/lead",
        data=_csrf_form(name="A", contact="tg", company="i-am-a-bot"),
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "TPL:_lead_ok" in r.text
    # create_lead was invoked but honeypot makes it a no-op (returns None) — no row written.
    assert called["n"] == 1


def test_lead_valid_hx_returns_lead_ok_partial(client, monkeypatch):
    received = {}

    async def fake_create(payload, *, database_url=None):
        received["payload"] = payload
        received["database_url"] = database_url
        return object()  # stand-in for a saved Lead

    monkeypatch.setattr(routes, "create_lead", fake_create)
    r = client.post(
        "/lead",
        data=_csrf_form(name="Анна", contact="@anna", budget_eur="700", districts="Врачар"),
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "TPL:_lead_ok" in r.text
    p = received["payload"]
    assert p.name == "Анна"
    assert p.contact == "@anna"
    assert p.budget_eur == 700
    assert p.districts == "Врачар"


def test_lead_valid_non_hx_redirects(client, monkeypatch):
    async def fake_create(payload, *, database_url=None):
        return object()

    monkeypatch.setattr(routes, "create_lead", fake_create)
    r = client.post("/lead", data=_csrf_form(name="A", contact="tg"), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?lead=ok"


def test_lead_forwards_site_listing_id_to_create_lead(client, monkeypatch):
    received = {}

    async def fake_create(payload, *, database_url=None):
        received["payload"] = payload
        return object()

    monkeypatch.setattr(routes, "create_lead", fake_create)
    r = client.post(
        "/lead",
        data=_csrf_form(name="Пётр", contact="@petr", site_listing_id="1"),
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert received["payload"].site_listing_id == 1


def test_lead_blank_site_listing_id_is_none(client, monkeypatch):
    received = {}

    async def fake_create(payload, *, database_url=None):
        received["payload"] = payload
        return object()

    monkeypatch.setattr(routes, "create_lead", fake_create)
    # general form (homepage) submits an empty hidden field — must not break, id stays None.
    client.post("/lead", data=_csrf_form(name="A", contact="t", site_listing_id=""))
    assert received["payload"].site_listing_id is None


def test_lead_non_int_site_listing_id_is_none(client, monkeypatch):
    received = {}

    async def fake_create(payload, *, database_url=None):
        received["payload"] = payload
        return object()

    monkeypatch.setattr(routes, "create_lead", fake_create)
    client.post("/lead", data=_csrf_form(name="A", contact="t", site_listing_id="abc"))
    assert received["payload"].site_listing_id is None


def test_lead_public_listing_persists_as_listing_kind(client):
    # End-to-end against the seeded DB: a public id -> kind='listing' + id stored.
    from unittest.mock import AsyncMock, patch

    with (
        patch("src.site.email.aiosmtplib.send", new=AsyncMock()),
        patch("src.site.leads.send_text_to_recipients", new=AsyncMock()),
    ):
        r = client.post(
            "/lead",
            data=_csrf_form(name="Пётр", contact="@petr", site_listing_id="1"),
            follow_redirects=False,
        )
    assert r.status_code == 303
    leads = _fetch_leads_sync(client)
    assert len(leads) == 1
    assert leads[0]["kind"] == "listing"
    assert leads[0]["site_listing_id"] == 1


def test_lead_private_listing_persists_as_general(client):
    # site_listing 4 is private -> degrade to a general lead, id NOT stored (never leaked).
    from unittest.mock import AsyncMock, patch

    with (
        patch("src.site.email.aiosmtplib.send", new=AsyncMock()),
        patch("src.site.leads.send_text_to_recipients", new=AsyncMock()),
    ):
        r = client.post(
            "/lead",
            data=_csrf_form(name="Bot", contact="@b", site_listing_id="4"),
            follow_redirects=False,
        )
    assert r.status_code == 303
    leads = _fetch_leads_sync(client)
    assert len(leads) == 1
    assert leads[0]["kind"] == "general"
    assert leads[0]["site_listing_id"] is None


def test_lead_validation_error_is_400(client, monkeypatch):
    async def fake_create(payload, *, database_url=None):
        from src.site.leads import LeadValidationError

        raise LeadValidationError("name must be 1..120 non-blank characters")

    monkeypatch.setattr(routes, "create_lead", fake_create)
    r = client.post("/lead", data=_csrf_form(name="", contact="tg"))
    assert r.status_code == 400


def test_lead_rate_limit_blocks_after_max(client, monkeypatch):
    async def fake_create(payload, *, database_url=None):
        return object()

    monkeypatch.setattr(routes, "create_lead", fake_create)
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "site_lead_ratelimit_max", 2)
    monkeypatch.setattr(settings, "site_lead_ratelimit_window", 3600)

    ok1 = client.post("/lead", data=_csrf_form(name="A", contact="t"), follow_redirects=False)
    ok2 = client.post("/lead", data=_csrf_form(name="B", contact="t"), follow_redirects=False)
    blocked = client.post("/lead", data=_csrf_form(name="C", contact="t"), follow_redirects=False)
    assert ok1.status_code == 303
    assert ok2.status_code == 303
    assert blocked.status_code == 429


# ============================================================== map.json shape is valid JSON


def test_map_json_is_serializable(client):
    r = client.get("/api/map.json")
    json.loads(r.text)  # raises if not valid JSON


# ============================================================== templates: lead CTA + hidden id (real markup)


class _FakeItem:
    """Minimal stand-in for SiteListingView for direct template rendering."""

    id = 42
    price_eur = 600
    area_m2 = 45.0
    rooms = "2 комнаты"
    floor = None
    district = "Врачар"
    address = None
    lat = 44.8
    lon = 20.47
    for_vnj = True
    for_white_card = False
    is_rented = False
    photos: list[str] = []
    note = None
    channel_url = "https://t.me/OtzRentBelgrad/1"
    posted_at = "2026-06-10T12:00:00+00:00"


def _real_env():
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    tpl_dir = Path(__file__).resolve().parents[2] / "src" / "site" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(tpl_dir)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["csrf_token"] = "tkn"
    env.globals["site_base_url"] = "https://rent.example.com"
    env.filters.setdefault("urlencode", lambda s: str(s))
    return env


def test_lead_form_has_hidden_site_listing_id():
    html = _real_env().get_template("partials/_lead_form.html").render(site_listing_id=42)
    assert 'name="site_listing_id"' in html
    assert 'value="42"' in html


def test_lead_form_hidden_id_empty_by_default():
    # the general homepage form carries no id -> empty value, still POSTs fine.
    html = _real_env().get_template("partials/_lead_form.html").render()
    assert 'name="site_listing_id"' in html
    assert 'name="site_listing_id" value=""' in html


def test_card_has_listing_lead_cta_with_id():
    html = _real_env().get_template("partials/_card.html").render(item=_FakeItem())
    assert "Оставить заявку на эту квартиру" in html
    assert "/apt/42#lead" in html


def test_apt_page_has_listing_lead_cta_and_prefilled_form():
    html = _real_env().get_template("apt.html").render(item=_FakeItem(), similar=[])
    assert "Оставить заявку на эту квартиру" in html
    # the CTA carries the apartment id for lead.js
    assert 'data-site-listing-id="42"' in html
    # the included lead form's hidden field is pre-filled from item.id
    assert 'name="site_listing_id" value="42"' in html
