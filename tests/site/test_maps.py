"""Stage S Wave 2 — fuzzy maps deep-links (DATA_CONTRACT-S §M, DECISIONS-S D1).

Coordinate ORDER is the classic bug, so it is asserted explicitly here:
  - Yandex `ll=`  → LON,LAT
  - Google `@`    → LAT,LON

FUZZY mode (public site, D1): neighborhood center, zoom 15, **no pin**. The precise
`pt=` Yandex pin is reserved for the TG-post pipeline (Дарья) and MUST stay intact.
"""

from __future__ import annotations

from html import unescape
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from jinja2 import DictLoader

import src.site.routes as routes
from src.post.location import yandex_maps_url
from src.site import cache
from src.site.app import create_app
from src.site.deps import catalog_db_url, lead_rate_limiter
from src.site.maps import MapsLinks, fuzz_coords, google_maps_url, maps_links


# --- fuzz_coords: ~100m privacy grid (D1) ----------------------------------------------


def test_fuzz_coords_snaps_to_3_decimals():
    # Building privacy: the exact coordinate must never reach the page; snap to a ~100m grid.
    lat, lon = fuzz_coords(44.812345, 20.456789)
    assert (lat, lon) == (44.812, 20.457)


def test_fuzz_coords_none_safe():
    assert fuzz_coords(None, 20.0) == (None, None)
    assert fuzz_coords(44.0, None) == (None, None)


def test_fuzz_coords_offset_stays_inside_150m_circle():
    # The snap offset (<~70m) must keep the true point inside the 150m fuzzy circle.
    flat, flon = fuzz_coords(44.812345, 20.456789)
    # ~111km per degree latitude; ~79km per degree longitude at Belgrade.
    dlat_m = abs(44.812345 - flat) * 111_000
    dlon_m = abs(20.456789 - flon) * 79_000
    assert (dlat_m**2 + dlon_m**2) ** 0.5 < 150


# --- maps_links: fuzzy URLs + coordinate order -----------------------------------------


def test_maps_links_returns_fuzzy_yandex_lon_lat_no_pin():
    links = maps_links(44.81, 20.47)
    assert isinstance(links, MapsLinks)
    # Yandex order is LON,LAT (20.47 then 44.81). Accept raw or %2C-encoded comma.
    assert ("ll=20.47,44.81" in links.yandex) or (f"ll=20.47{quote(',')}44.81" in links.yandex)
    assert "z=15" in links.yandex
    # FUZZY: no precise pin marker.
    assert "pt=" not in links.yandex


def test_maps_links_returns_fuzzy_google_lat_lon_center():
    links = maps_links(44.81, 20.47)
    # Google order is LAT,LON (44.81 then 20.47), neighborhood center, no precise pin.
    assert links.google == "https://www.google.com/maps/@44.81,20.47,15z"
    # FUZZY center, not the precise search endpoint.
    assert "search" not in links.google
    assert "query=" not in links.google


def test_maps_links_none_when_lat_missing():
    assert maps_links(None, 20.0) is None


def test_maps_links_none_when_lon_missing():
    assert maps_links(44.0, None) is None


def test_google_maps_url_precise_variant_uses_search_query():
    # Reserved for the future; LAT,LON order in the query.
    assert (
        google_maps_url(44.81, 20.47, precise=True)
        == "https://www.google.com/maps/search/?api=1&query=44.81,20.47"
    )


# --- regression: TG-post Yandex path (precise=True default) is UNCHANGED ----------------


def test_yandex_maps_url_default_precise_keeps_pin_and_zoom17():
    url = yandex_maps_url(lat=44.81, lon=20.47)
    assert url is not None
    # precise default → pin (LON,LAT) + zoom 17, exactly as the TG pipeline relies on.
    assert "pt=20.47,44.81" in url
    assert "z=17" in url


def test_yandex_maps_url_precise_false_drops_pin_uses_zoom15():
    url = yandex_maps_url(lat=44.81, lon=20.47, precise=False, zoom=15)
    assert url is not None
    assert "ll=20.47,44.81" in url
    assert "z=15" in url
    assert "pt=" not in url


# --- route-level: /apt/{id} carries fuzzy maps for a listing WITH coords ----------------

# Stub templates echo coords/maps so we assert the context the handler emits without
# depending on the concurrently-edited apt.html markup.
_APT_STUBS = {
    "base.html": "BASE",
    "apt.html": (
        "TPL:apt id={{ item.id }} "
        "coords={{ coords }} "
        "yandex={{ maps.yandex if maps else 'NONE' }} "
        "google={{ maps.google if maps else 'NONE' }}"
    ),
}


@pytest.fixture
def _stub_apt_templates(monkeypatch):
    monkeypatch.setattr(routes.templates.env, "loader", DictLoader(_APT_STUBS))
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


def test_apt_with_coords_carries_fuzzy_maps(client, _stub_apt_templates):
    # Seed listing id=1 has lat=44.8, lon=20.47. Jinja autoescapes the echoed context
    # ('→&#39;, &→&amp;), so unescape before asserting on the underlying values.
    r = client.get("/apt/1")
    assert r.status_code == 200
    text = unescape(r.text)
    # coords dict present.
    assert "coords={'lat': 44.8, 'lon': 20.47}" in text
    # Yandex fuzzy: LON,LAT, no pin.
    assert "ll=20.47,44.8" in text
    assert "pt=" not in text
    # Google fuzzy: LAT,LON center.
    assert "google=https://www.google.com/maps/@44.8,20.47,15z" in text


def test_apt_without_coords_has_no_maps(client, _stub_apt_templates):
    # Seed listing id=2 has lat=None, lon=None.
    r = client.get("/apt/2")
    assert r.status_code == 200
    text = unescape(r.text)
    assert "coords=None" in text
    assert "yandex=NONE" in text
    assert "google=NONE" in text
