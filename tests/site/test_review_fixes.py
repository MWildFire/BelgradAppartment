"""Regression tests for the multi-agent code/design review fixes.

Covers the behavioral changes made in response to the review:
- readmodel.public_ids (sitemap id-only query) + similar() honoring C1 (no rented).
- Template/a11y hooks: HTMX in-flight feedback + double-submit guard, results live region,
  required-field markers, nav active-state, map-pin contrast fix,
  color-scheme, and the moved-out favorites styles.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

import src.site.readmodel as rm

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = ROOT / "src" / "site" / "templates"
STATIC = ROOT / "src" / "site" / "static"


# --------------------------------------------------------------------------- readmodel


@pytest.mark.asyncio
async def test_public_ids_lists_public_primary_only(site_db: str) -> None:
    """public_ids returns every public+primary site_listing id and never a private one."""
    ids = await rm.public_ids(database_url=site_db)
    assert set(ids) == {1, 2, 3}  # id 4's site_listing is is_public=False
    assert 4 not in ids


@pytest.mark.asyncio
async def test_similar_excludes_rented_and_self(site_db: str) -> None:
    """C1: «похожие» never surfaces a rented listing (id=3 is rented) nor the listing itself."""
    base = await rm.get_public(1, database_url=site_db)
    assert base is not None
    sims = await rm.similar(base, limit=4, database_url=site_db)
    assert all(not v.is_rented for v in sims)
    assert all(v.id != base.id for v in sims)


# --------------------------------------------------------------------------- templates


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["site_base_url"] = "https://rent.example.com"
    return env


def _render(name: str, **ctx: object) -> str:
    return _env().get_template(name).render(**ctx)


def test_base_declares_light_color_scheme() -> None:
    html = _render("base.html", csrf_token="x")
    assert '<meta name="color-scheme" content="light">' in html


def test_base_inline_favorites_style_moved_to_css() -> None:
    """The favorites .fav block no longer ships as an inline <style> in base.html …"""
    html = _render("base.html", csrf_token="x")
    assert "<style>" not in html, "base.html should carry no inline <style> block"
    # … it lives in site.css instead.
    css = (STATIC / "css" / "site.css").read_text(encoding="utf-8")
    assert ".fav{" in css and ".fav-count{" in css


def test_lead_form_has_double_submit_guard_and_required_markers() -> None:
    html = _render("partials/_lead_form.html", csrf_token="x", site_listing_id="")
    assert 'hx-disabled-elt' in html  # UX-2: submit disabled while in flight
    assert html.count('aria-required="true"') >= 2  # UX-5: name + contact
    assert 'class="req"' in html  # visible required marker


def test_results_count_is_a_live_region() -> None:
    ctx = {
        "items": [],
        "total": 0,
        "applied": {
            "vnj": False, "wc": False, "district": [], "price_min": None,
            "price_max": None, "rooms": [], "sort": "new", "show_rented": False,
        },
        "pager": {"total_pages": 1, "page": 1, "has_prev": False, "has_next": False,
                  "qs_prev": "", "qs_next": ""},
    }
    html = _render("partials/_results.html", **ctx)
    # W5-22: the live region moved OUT of the swap target (a node recreated by every swap is
    # announced unreliably) — _results.html now updates the persistent #results-status in
    # catalog.html via hx-swap-oob; the filter form still drives the #results indicator.
    assert 'hx-swap-oob="innerHTML:#results-status"' in html
    assert 'hx-indicator="#results"' in html
    catalog_ctx = {**ctx, "facets": {"districts": [], "price_bounds": (0, 0), "rooms": [],
                                     "counts": {"vnj": 0, "white_card": 0, "total": 0}},
                   "csrf_token": "x"}
    catalog = _render("catalog.html", **catalog_ctx)
    assert 'id="results-status"' in catalog
    assert 'aria-live="polite"' in catalog


def test_filters_form_drives_results_indicator() -> None:
    html = _render(
        "partials/_filters.html",
        facets={"districts": [], "price_bounds": (0, 0),
                "counts": {"vnj": 0, "white_card": 0, "total": 0}},
        applied={"vnj": False, "wc": False, "district": [], "price_min": None,
                 "price_max": None, "rooms": [], "sort": "new", "show_rented": False},
    )
    assert 'hx-indicator="#results"' in html


def test_map_price_pins_use_dark_text_on_light_buckets() -> None:
    """VIS-1: the two LIGHT price buckets carry dark --ink text (near-white was ~2:1)."""
    html = _render("map.html", csrf_token="x")
    assert ".price-pin.bucket-1{background:var(--ochre);color:var(--ink)}" in html
    assert ".price-pin.bucket-2{background:#C9743C;color:var(--ink)}" in html


def test_apt_telegram_channel_link_opens_new_tab() -> None:
    from tests.site.test_a11y import FakeView

    html = _render("apt.html", item=FakeView(), similar=[], csrf_token="x")
    # the «Открыть в Telegram» channel link matches every other TG link (new tab).
    assert 'href="https://t.me/RentBelgrad/128" target="_blank"' in html


def test_apt_rented_listing_shows_banner() -> None:
    from tests.site.test_a11y import FakeView

    html = _render("apt.html", item=FakeView(is_rented=True), similar=[], csrf_token="x")
    assert "rented-banner" in html
    assert "уже снята" in html
