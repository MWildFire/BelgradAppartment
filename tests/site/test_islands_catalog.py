"""Wave 4 island wiring — render-smoke + pagination route tests (agent E).

Two layers, mirroring the project's existing split:
- PAGINATION route tests go through the real catalog handler (seeded DB) and assert the
  *slice* + the pager context keys the handler now emits — JS-agnostic, server logic only.
- RENDER-SMOKE tests render the real templates through a plain Jinja env (like test_render.py)
  and assert the island HOOKS exist: self-hosted htmx <script>, hx-get on filters, pager links
  gated on total_pages>1, and the Leaflet/#map mount on the map page. JS RUNTIME behavior
  (debounce, marker clustering, clear-all) is Wave 5 Playwright, NOT asserted here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient
from jinja2 import DictLoader, Environment, FileSystemLoader, select_autoescape

import src.site.routes as routes
from src.config import get_settings
from src.site import cache
from src.site.app import create_app
from src.site.deps import catalog_db_url, lead_rate_limiter

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "site" / "templates"
STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "site" / "static"


# =====================================================================================
# Pagination — real catalog handler over the seeded DB (server logic only).
# =====================================================================================

# Stub templates that echo the pager context so the route's slice + pager keys are
# assertable without depending on agent D/F template markup.
_PAGER_STUBS = {
    "base.html": "BASE",
    "catalog.html": (
        "TPL:catalog items={{ items|length }} total={{ total }} "
        "page={{ pager.page }} pages={{ pager.total_pages }} "
        "prev={{ pager.has_prev }} next={{ pager.has_next }} "
        "qsprev={{ pager.qs_prev }} qsnext={{ pager.qs_next }}"
    ),
    "partials/_results.html": (
        "TPL:_results items={{ items|length }} page={{ pager.page }} "
        "pages={{ pager.total_pages }} qsnext={{ pager.qs_next }}"
    ),
    "partials/_filters.html": "TPL:_filters",
    "partials/_card.html": "TPL:_card id={{ item.id }}",
}


@pytest.fixture
def _stub_pager_templates(monkeypatch):
    monkeypatch.setattr(routes.templates.env, "loader", DictLoader(_PAGER_STUBS))
    yield


@pytest.fixture(autouse=True)
def _clear_state():
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


@pytest.fixture
def small_page(monkeypatch):
    """Shrink page size so the 3-public seed spans >1 page (page_size=2 → 2 pages)."""
    monkeypatch.setattr(get_settings(), "site_page_size", 2)
    yield 2


def test_catalog_page1_has_next_not_prev(client, small_page, _stub_pager_templates):
    r = client.get("/catalog", params={"page": "1"})
    assert r.status_code == 200
    # 3 public listings, page_size 2 → page 1 has 2 items, total_pages 2.
    assert "items=2" in r.text
    assert "total=3" in r.text
    assert "page=1" in r.text
    assert "pages=2" in r.text
    assert "prev=False" in r.text
    assert "next=True" in r.text


def test_catalog_page2_returns_remaining_slice(client, small_page, _stub_pager_templates):
    r = client.get("/catalog", params={"page": "2"})
    assert r.status_code == 200
    # page 2 holds the 1 remaining listing; has_prev, no has_next.
    assert "items=1" in r.text
    assert "page=2" in r.text
    assert "prev=True" in r.text
    assert "next=False" in r.text


def test_pager_qs_preserves_active_filters(client, small_page, _stub_pager_templates):
    # vnj filter narrows to 1 public listing → 1 page; assert the qs CARRIES the filter
    # even when there is no next page (qs is built independent of bounds).
    r = client.get("/catalog", params={"sort": "price_asc"})
    assert r.status_code == 200
    # extract qsnext from the echoed stub
    line = r.text
    marker = "qsnext="
    # the stub echoes the context value through Jinja autoescape; the underlying qs joins with
    # raw '&' (browsers decode the &amp; in real attributes), so unescape before parsing.
    qsnext = unescape(line.split(marker, 1)[1].split()[0])
    parsed = parse_qs(qsnext)
    assert parsed.get("sort") == ["price_asc"]
    assert parsed.get("page") == ["2"]  # next points to page+1, filters preserved


def test_pager_qs_prev_points_back_and_keeps_filters(client, small_page, _stub_pager_templates):
    r = client.get(
        "/catalog",
        params=[("page", "2"), ("district", "Vracar"), ("vnj", "1")],
    )
    assert r.status_code == 200
    qsprev = unescape(r.text.split("qsprev=", 1)[1].split()[0])
    parsed = parse_qs(qsprev)
    assert parsed.get("page") == ["1"]
    assert parsed.get("district") == ["Vracar"]
    assert parsed.get("vnj") == ["1"]


def test_catalog_single_page_has_no_neighbors(client, _stub_pager_templates):
    # default page_size (24) >> 3 public → one page, no prev/next.
    r = client.get("/catalog")
    assert r.status_code == 200
    assert "pages=1" in r.text
    assert "prev=False" in r.text
    assert "next=False" in r.text


# =====================================================================================
# Render-smoke — real templates; assert the island HOOKS (not JS runtime behavior).
# =====================================================================================


@dataclass(frozen=True)
class FakeView:
    id: int = 128
    price_eur: int = 650
    area_m2: float | None = 48.0
    rooms: str = "1+1"
    floor: str | None = None
    district: str = "Врачар"
    address: str | None = None
    lat: float | None = 44.8
    lon: float | None = 20.47
    for_vnj: bool = True
    for_white_card: bool = True
    is_rented: bool = False
    photos: list[str] = field(default_factory=lambda: ["/photos/a.webp"])
    note: str | None = None
    channel_url: str = "https://t.me/RentBelgrad/128"
    posted_at: str = "2026-06-18T10:00:00"


_FACETS = {
    "districts": [("Врачар", 7), ("Дорћол", 3)],
    "price_bounds": (450, 900),
    "rooms": [("1+1", 5)],
    "counts": {"vnj": 6, "white_card": 4, "total": 10},
}
_APPLIED = {
    "vnj": True, "wc": False, "district": ["Врачар"], "price_min": None,
    "price_max": 700, "rooms": ["1"], "sort": "new", "page": 1, "show_rented": False,
}


def _file_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def _render(name: str, **ctx: object) -> str:
    return _file_env().get_template(name).render(**ctx)


def _pager(page=1, total_pages=1, qs_prev="page=0", qs_next="page=2"):
    return {
        "page": page, "page_size": 24, "total_pages": total_pages,
        "has_prev": page > 1, "has_next": page < total_pages,
        "qs_prev": qs_prev, "qs_next": qs_next,
    }


# --- htmx self-host -------------------------------------------------------------------

def test_base_includes_self_hosted_htmx_script() -> None:
    html = _render("base.html")
    assert '/static/js/htmx.min.js' in html
    # no external script CDN for htmx (self-host policy)
    assert "unpkg.com" not in html
    assert "cdn.jsdelivr" not in html or "leaflet" in html.lower()


def test_htmx_file_is_present_and_nonempty() -> None:
    f = STATIC_DIR / "js" / "htmx.min.js"
    assert f.exists(), "htmx.min.js must be self-hosted under static/js"
    assert f.stat().st_size > 10_000, "htmx.min.js looks too small to be the real bundle"


# --- filters hooks --------------------------------------------------------------------

def test_filters_partial_has_hx_get_and_toggles() -> None:
    html = _render("partials/_filters.html", facets=_FACETS, applied=_APPLIED)
    assert 'hx-get="/catalog"' in html
    assert 'hx-target="#results"' in html
    assert 'name="vnj"' in html  # ВНЖ toggle
    assert 'name="wc"' in html   # белый картон toggle
    # no-JS fallback submit must survive
    assert "<noscript>" in html


def test_filters_partial_references_filters_js() -> None:
    # progressive-enhancement script wired (deferred); presence is the hook, behavior is Wave 5.
    html = _render("partials/_filters.html", facets=_FACETS, applied=_APPLIED)
    assert "filters.js" in html


def test_filters_js_self_hosted_nonempty() -> None:
    f = STATIC_DIR / "js" / "filters.js"
    assert f.exists()
    assert f.stat().st_size > 0


# --- pager hooks ----------------------------------------------------------------------

def test_results_renders_pager_when_multiple_pages() -> None:
    html = _render(
        "partials/_results.html",
        items=[FakeView()],
        facets=_FACETS,
        applied=_APPLIED,
        total=30,
        pager=_pager(page=1, total_pages=2, qs_next="vnj=1&page=2"),
    )
    assert 'class="pager"' in html
    # next link is both a plain deep-link (href) AND htmx (hx-get → #results)
    assert "vnj=1&amp;page=2" in html or "vnj=1&page=2" in html
    assert 'hx-target="#results"' in html


def test_results_no_pager_on_single_page() -> None:
    html = _render(
        "partials/_results.html",
        items=[FakeView()],
        facets=_FACETS,
        applied=_APPLIED,
        total=1,
        pager=_pager(page=1, total_pages=1),
    )
    assert 'class="pager"' not in html


def test_results_pager_prev_link_on_later_page() -> None:
    html = _render(
        "partials/_results.html",
        items=[FakeView()],
        facets=_FACETS,
        applied={**_APPLIED, "page": 2},
        total=30,
        pager=_pager(page=2, total_pages=2, qs_prev="vnj=1&page=1"),
    )
    assert "назад" in html  # prev control rendered
    assert "vnj=1&amp;page=1" in html or "vnj=1&page=1" in html


# --- map hooks ------------------------------------------------------------------------

def test_map_mounts_container_and_references_leaflet_and_mapjs() -> None:
    html = _render("map.html")
    assert 'id="map"' in html
    assert 'data-map-src="/api/map.json"' in html
    assert "leaflet" in html.lower()       # Leaflet CSS+JS (plan-sanctioned CDN)
    assert "/static/js/map.js" in html     # self-hosted island script


def test_map_js_self_hosted_nonempty() -> None:
    f = STATIC_DIR / "js" / "map.js"
    assert f.exists()
    assert f.stat().st_size > 0
