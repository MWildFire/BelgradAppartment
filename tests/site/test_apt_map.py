"""Unit tests for the /apt/{id} fuzzy mini-map block + Yandex/Google deep-link buttons.

Stage S, Wave 2 (S2-2 / S2-3), DATA_CONTRACT-S §M (режим ФУЗЗИ / D1).

These render the REAL apt.html through a FileSystemLoader env (mirroring test_routes.py's
_real_env), passing `coords`/`maps` ourselves exactly as routes.py does. We assert:
  - WITH coords+maps → the Leaflet mount (#apt-map) is present carrying data-lat/data-lon,
    apt-map.js + Leaflet CDN (SRI) are wired, and both deep-link anchors carry the exact
    maps.yandex / maps.google hrefs with target=_blank rel=noopener.
  - WITHOUT coords → NO map mount, NO deep-link anchors, and the «Точный адрес уточняйте»
    fallback (район + Telegram CTA) is shown instead.
  - ToS invariant: the deep-links are coordinate-area Yandex/Google URLs only — never a площадка.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.site.maps import maps_links


class _FakeItem:
    """Minimal stand-in for SiteListingView for direct template rendering (mirrors test_routes)."""

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


def _real_env() -> Environment:
    tpl_dir = Path(__file__).resolve().parents[2] / "src" / "site" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(tpl_dir)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["csrf_token"] = "tkn"
    env.globals["site_base_url"] = "https://rent.example.com"
    env.globals["telegram_url"] = "https://t.me/Darya_kralj"
    env.filters.setdefault("urlencode", lambda s: str(s))
    return env


def _render_with_coords() -> str:
    item = _FakeItem()
    coords = {"lat": item.lat, "lon": item.lon}
    maps = maps_links(item.lat, item.lon)
    return _real_env().get_template("apt.html").render(
        item=item, similar=[], coords=coords, maps=maps
    )


def _render_no_coords() -> str:
    item = _FakeItem()
    item.lat = None
    item.lon = None
    # routes.py builds coords=None and maps=None when lat/lon are absent.
    return _real_env().get_template("apt.html").render(
        item=item, similar=[], coords=None, maps=None
    )


# ----------------------------------------------------------------- WITH coords


def test_apt_map_mount_present_with_coords():
    html = _render_with_coords()
    # The Leaflet mount carries the coords as data-attrs (CSP-safe hand-off to apt-map.js).
    assert 'id="apt-map"' in html
    assert 'data-lat="44.8"' in html
    assert 'data-lon="20.47"' in html


def test_apt_map_wires_leaflet_and_island_with_coords():
    html = _render_with_coords()
    # Leaflet CSS + JS from jsdelivr with SRI (same pattern as map.html), plus our island.
    assert "cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css" in html
    assert "cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js" in html
    assert "integrity=" in html  # SRI present on the Leaflet includes
    assert "/static/js/apt-map.js?v=" in html  # cache-busted island
    assert "/static/css/apt.css?v=" in html  # cache-busted apt-page styles


def test_apt_deeplink_buttons_have_exact_hrefs_with_coords():
    from markupsafe import escape

    item = _FakeItem()
    maps = maps_links(item.lat, item.lon)
    html = _render_with_coords()
    assert maps is not None
    # Exact hrefs from maps.yandex / maps.google. Jinja autoescapes the URL inside the attribute
    # (e.g. `&` → `&amp;`) — exactly as the production response does — so compare the escaped form.
    assert f'href="{escape(maps.yandex)}"' in html
    assert f'href="{escape(maps.google)}"' in html
    # Labels (RU, per §M).
    assert "Открыть в Yandex Maps" in html
    assert "Открыть в Google Maps" in html
    # New tab + safe rel on BOTH external anchors.
    assert html.count('target="_blank"') >= 2
    assert html.count('rel="noopener"') >= 2


def test_apt_deeplinks_are_coordinate_area_only_no_площадка():
    """ToS invariant: both deep-links open a Yandex/Google coordinate area — never a площадка."""
    html = _render_with_coords()
    assert "yandex.com/maps/" in html
    assert "www.google.com/maps/" in html
    for bad in ("nekretnine.rs", "4zida.rs", "halooglasi.com", "cdn-telegram", "cdn/remote"):
        assert bad not in html


# ----------------------------------------------------------------- WITHOUT coords


def test_apt_no_map_mount_when_coords_none():
    html = _render_no_coords()
    assert 'id="apt-map"' not in html
    # No Leaflet wiring + no external deep-links when there are no coords.
    assert "apt-map.js" not in html
    assert "cdn.jsdelivr.net/npm/leaflet" not in html
    assert "Открыть в Yandex Maps" not in html
    assert "Открыть в Google Maps" not in html


def test_apt_fallback_text_when_coords_none():
    html = _render_no_coords()
    # §M no-coords behaviour: «Точный адрес уточняйте» + район + Telegram CTA.
    assert "Точный адрес уточняйте" in html
    assert "Врачар" in html  # the район is shown
    assert "https://t.me/Darya_kralj" in html  # Telegram CTA


# ----------------------------------------------------------------- regression: existing CTA intact


def test_apt_existing_lead_cta_survives_map_block():
    html = _render_with_coords()
    assert "Оставить заявку на эту квартиру" in html
    assert 'data-site-listing-id="42"' in html
    assert 'name="site_listing_id" value="42"' in html
