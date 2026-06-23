"""Render-smoke tests for the public-catalog templates (Stage W, W3-D).

These exercise template MARKUP only: each template is rendered through a plain Jinja2
Environment pointed at ``src/site/templates`` with a hand-built context that mimics the
DATA_CONTRACT §1 ``SiteListingView`` fields + §2 context keys. We deliberately do NOT
import ``src.site.routes`` (owned by sibling agent C, possibly mid-edit) — the contract,
not the route module, is the interface under test here.

Assertions are intentionally about load-bearing DoD nodes (anti-slop / contract), not pixels:
price with €, ВНЖ badge, channel link, «снято» badge, lead-form honeypot + CSRF field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "site" / "templates"


@dataclass(frozen=True)
class FakeView:
    """Mirror of SiteListingView (DATA_CONTRACT §1) — only the fields templates may read."""

    id: int = 128
    price_eur: int = 650
    area_m2: float | None = 48.0
    rooms: str = "1+1"
    floor: str | None = "3/5"
    district: str = "Врачар"
    address: str | None = None  # contract: always None for MVP, must not be rendered
    lat: float | None = 44.8
    lon: float | None = 20.47
    for_vnj: bool = True
    for_white_card: bool = True
    is_rented: bool = False
    photos: list[str] = field(default_factory=lambda: ["/photos/a.webp", "/photos/b.webp"])
    note: str | None = "Хозяин оформит ugovor на 12 месяцев."
    channel_url: str = "https://t.me/RentBelgrad/128"
    posted_at: str = "2026-06-18T10:00:00"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


# --- shared fake context blocks (match §2 keys) ---

_FACETS = {
    "districts": [("Врачар", 7), ("Дорћол", 3)],
    "price_bounds": (450, 900),
    "rooms": [("1+1", 5), ("студия", 2), ("2+1", 1)],
    "counts": {"vnj": 6, "white_card": 4, "total": 10},
}
_APPLIED = {
    "vnj": True,
    "wc": False,
    "district": ["Врачар"],
    "price_min": None,
    "price_max": 700,
    "rooms": ["1"],
    "sort": "new",
    "show_rented": False,
}
_STATS = {"active": 47, "districts": 8}
# C renders ``csrf_token`` (admin Jinja-global pattern, "как в админке"); provide it here.
_CSRF = "test-csrf-token"


def _render(name: str, **ctx: object) -> str:
    return _env().get_template(name).render(**ctx)


def test_app_registers_telegram_url_global() -> None:
    """F5: importing src.site.app must register a Jinja global `telegram_url`
    (= settings.site_telegram_url) on the shared `templates` env, so every page
    can emit the «Написать в Telegram» CTA via {{ telegram_url }}."""
    import src.site.app  # noqa: F401 — import side-effect registers the global
    from src.config import get_settings
    from src.site.routes import templates

    assert templates.env.globals.get("telegram_url") == get_settings().site_telegram_url


def test_base_has_telegram_cta_in_three_places_with_a11y() -> None:
    """F5: «Написать в Telegram» CTA appears in header + footer + floating sticky
    (≥3 occurrences of the DM url), each opens in a new tab safely, the icon-only
    variants carry an aria-label, and no emoji char is used (inline mono SVG only)."""
    tg = "https://t.me/Darya_kralj"
    html = _render(
        "index.html",
        featured=[FakeView()],
        stats=_STATS,
        csrf_token=_CSRF,
        telegram_url=tg,
    )
    # header + footer + sticky = at least 3 links to Дарья's DM
    assert html.count(tg) >= 3
    # opens safely in a new tab
    assert 'target="_blank"' in html
    assert 'rel="noopener"' in html
    # accessible label for the (icon-only) Telegram CTA
    assert 'aria-label="Написать в Telegram"' in html
    # anti-slop: NO emoji glyph anywhere in the markup (inline mono SVG instead)
    assert "✈" not in html and "📲" not in html and "📩" not in html and "💬" not in html


# --------------------------------------------------------------------------- base
def test_base_has_no_external_font_link() -> None:
    """Anti-slop / contract: fonts are self-hosted; no fonts.googleapis <link> ships."""
    html = _render("base.html")
    assert "/static/css/tokens.css" in html
    assert "/static/css/site.css" in html
    assert "fonts.googleapis" not in html


# ------------------------------------------------------------- W3-3 content pages
def test_base_nav_links_point_to_vnj_and_how_pages() -> None:
    """W3-3: the «ВНЖ и договор» / «Как работаем» nav links resolve to the new
    standalone pages /vnj and /how (no longer the homepage anchors /#vnj, /#how)."""
    html = _render("base.html", telegram_url="https://t.me/Darya_kralj")
    assert 'href="/vnj"' in html
    assert 'href="/how"' in html
    # the nav must not still point those two items at the old homepage anchors
    assert 'href="/#vnj"' not in html
    assert 'href="/#how"' not in html


def test_vnj_page_renders_contract_content() -> None:
    """W3-3: «ВНЖ и договор» content page — extends base, carries the checklist of
    what a lease needs (official registered ugovor), a common-mistakes section, and
    a Telegram CTA. Only general, non-invented statements (Дарья verifies specifics)."""
    tg = "https://t.me/Darya_kralj"
    html = _render("vnj.html", telegram_url=tg)
    assert "договор" in html  # the page is about the lease/ugovor
    assert "ugovor" in html  # official Serbian term, consistent with site copy
    assert "боравиште" in html or "регистрац" in html  # address registration angle
    assert tg in html  # Telegram CTA uses the global


def test_how_page_renders_commission_breakdown() -> None:
    """W3-3 / F3 (DECISIONS-W2): «Как работаем» carries the EXACT commission breakdown
    — 50% of one month's rent, one-time, on move-in — plus a Telegram CTA. No «бесплатно»
    / «платит хозяин» falsehoods (commission-honesty sweep)."""
    tg = "https://t.me/Darya_kralj"
    html = _render("how.html", telegram_url=tg)
    assert "50%" in html
    assert "месячной аренды" in html
    assert "при заселении" in html
    assert tg in html  # Telegram CTA uses the global
    # no commission-honesty regressions
    assert "бесплат" not in html
    assert "платит хозяин" not in html


# -------------------------------------------------------------------------- index
def test_index_hero_headline_is_klyuchi_ot_belgrada() -> None:
    """F1 (DECISIONS-W2): hero <h1> reads «Ключи от Белграда»; the old
    «по-человечески» headline (and its manual <br> hack) is gone. Subheading kept."""
    html = _render(
        "index.html", featured=[FakeView()], stats=_STATS, csrf_token=_CSRF
    )
    # «Белграда» carries the oxblood .ox accent span, so the headline is split by
    # inline markup — assert on the visible text (tags stripped), not raw HTML.
    visible = re.sub(r"<[^>]+>", "", html).lower()
    assert "ключи от белграда" in visible
    assert "по-человеческ" not in visible
    assert "Помогаем релокантам" in html  # hero-lede subheading unchanged


def test_index_commission_stat_is_honest_50pct() -> None:
    """F3 (DECISIONS-W2): the 3rd hero stat is no longer the «€0 / комиссия с тебя»
    lie. It reads «50% / комиссия за подбор» (mono numeric, per sibling-stat
    convention). The first two stats stay dynamic (3 stats in the row)."""
    html = _render(
        "index.html", featured=[FakeView()], stats=_STATS, csrf_token=_CSRF
    )
    assert "50%" in html
    assert "комиссия за подбор" in html
    # the old dishonest stat is gone
    assert "€0" not in html
    assert "комиссия с тебя" not in html
    # first two stats remain dynamic, still 3 stats in the row
    assert "47" in html  # stats.active rendered
    assert "8" in html  # stats.districts rendered
    assert html.count('class="stat"') == 3


def test_index_checks_04_no_longer_contradicts_50pct_commission() -> None:
    """F3 (continuation, DECISIONS-W2): the «почему мы» check-list #04 used to claim
    «Комиссию платит хозяин — … бесплатны», which contradicts the honest 50% hero
    stat. It must be reworded to a true value-prop (transparency / no hidden fees)
    that does NOT say the service is free or that the landlord pays. The block keeps
    all 4 checks and the 50% hero stat is untouched."""
    html = _render(
        "index.html", featured=[FakeView()], stats=_STATS, csrf_token=_CSRF
    )
    # the contradicting / false claim is gone
    assert "Комиссию платит хозяин" not in html
    assert "бесплатны" not in html
    assert "бесплат" not in html  # no «бесплатно/бесплатный» anywhere either
    # the check-list still has exactly 4 items, with «04» preserved
    assert html.count('class="c"') == 4
    assert ">04<" in html
    # no regression on the honest hero stat (F3)
    assert "50%" in html
    assert "комиссия за подбор" in html


def test_index_hero_week_card_renders_real_apartment() -> None:
    """W3-2: the hero side-card («квартира недели») renders the real `week` apartment —
    its photo (with alt), price (mono, with €), Cyrillic district, and a «подробнее»
    link to /apt/{week.id}."""
    week = FakeView(id=512, price_eur=720, district="Дорћол")
    html = _render(
        "index.html", featured=[FakeView()], stats=_STATS, csrf_token=_CSRF, week=week
    )
    # the hero side-card links to the featured apartment's detail page
    assert "/apt/512" in html
    assert "подробнее" in html
    # price with € and the numeric value rendered for the week card
    assert "€" in html
    assert "720" in html
    # Cyrillic district
    assert "Дорћол" in html
    # the week photo is shown with alt text
    assert 'src="/photos/a.webp"' in html
    assert "alt=" in html
    # placeholder copy must NOT show when a real week apartment is present
    assert "скоро здесь" not in html


def test_index_hero_week_card_placeholder_when_none() -> None:
    """W3-2: when `week` is None the hero side-card keeps the «скоро здесь» placeholder
    and emits no /apt detail link from that card."""
    html = _render(
        "index.html", featured=[FakeView()], stats=_STATS, csrf_token=_CSRF, week=None
    )
    assert "скоро здесь" in html
    # no «подробнее» CTA for a non-existent week apartment
    assert "подробнее" not in html


def test_index_renders_featured_and_stats() -> None:
    html = _render(
        "index.html", featured=[FakeView()], stats=_STATS, csrf_token=_CSRF
    )
    assert "€" in html
    assert "650" in html
    assert "ВНЖ" in html  # vnj badge on the featured card
    assert "47" in html  # stats.active
    # lead form on the homepage: honeypot + csrf
    assert 'name="company"' in html
    assert _CSRF in html


# ------------------------------------------------------------------------ catalog
def test_catalog_renders_items_filters_results() -> None:
    html = _render(
        "catalog.html",
        items=[FakeView(), FakeView(id=129, is_rented=True, price_eur=550)],
        facets=_FACETS,
        applied=_APPLIED,
        total=2,
        csrf_token=_CSRF,
    )
    assert "€" in html
    assert "ВНЖ" in html  # prominent vnj toggle in filters
    assert "снято" in html  # rented card shown muted, not hidden


def test_results_partial_is_grid_only() -> None:
    """partials/_results.html is the HTMX swap target: grid + pagination only, no <nav> chrome."""
    html = _render(
        "partials/_results.html",
        items=[FakeView()],
        facets=_FACETS,
        applied=_APPLIED,
        total=1,
    )
    assert "650" in html
    assert "€" in html
    # swap target must not carry the site nav/header
    assert "<header" not in html


# ---------------------------------------------------------------------- partials
def test_card_partial_core_nodes() -> None:
    html = _render("partials/_card.html", item=FakeView())
    assert "€" in html and "650" in html
    assert "48" in html  # area
    assert "1+1" in html  # rooms
    assert "ВНЖ" in html  # vnj badge
    assert "/apt/128" in html  # link to detail
    # alt text present on photo
    assert "alt=" in html
    # ToS: never emit a source/platform badge
    assert "Telegram" not in html
    assert "src=\"/photos/a.webp\"" in html


def test_card_partial_rented_badge_shown_not_hidden() -> None:
    html = _render("partials/_card.html", item=FakeView(is_rented=True))
    assert "снято" in html  # muted badge, card still present
    assert "650" in html


def test_card_partial_white_card_badge() -> None:
    html = _render("partials/_card.html", item=FakeView(for_white_card=True))
    assert "картон" in html


def test_filters_partial_prominent_toggles() -> None:
    html = _render("partials/_filters.html", facets=_FACETS, applied=_APPLIED)
    assert "ВНЖ" in html
    assert "картон" in html  # белый картон toggle
    assert 'name="vnj"' in html
    assert 'name="wc"' in html
    assert "Врачар" in html  # district facet


# --- C1: «показать снятые» toggle + photo fallback ---


def test_filters_partial_has_show_rented_toggle() -> None:
    """C1: the filter rail carries a «показать снятые» toggle bound to show_rented; unchecked
    by default (rented hidden), checked when applied.show_rented is True (deep-link round-trip)."""
    off = _render("partials/_filters.html", facets=_FACETS, applied=_APPLIED)
    assert 'name="show_rented"' in off
    assert "снятые" in off  # «показать снятые» label
    # default applied.show_rented is False -> the checkbox is not pre-checked
    assert "checked" not in off.split('name="show_rented"')[0].rsplit("<", 1)[-1]

    applied_on = {**_APPLIED, "show_rented": True}
    on = _render("partials/_filters.html", facets=_FACETS, applied=applied_on)
    # round-trips the active state so a deep-link with show_rented=1 keeps the toggle on
    assert re.search(r'name="show_rented"[^>]*\bchecked', on) or re.search(
        r'\bchecked[^>]*name="show_rented"', on
    )


def test_card_photo_has_onerror_fallback_to_placeholder() -> None:
    """C1: a 404'ing photo must not break the grid — <img onerror> swaps to the committed
    placeholder asset (and clears its own onerror so it can't loop)."""
    html = _render("partials/_card.html", item=FakeView())
    assert "onerror=" in html
    assert "/static/img/placeholder.svg" in html


def test_apt_photo_has_onerror_fallback_to_placeholder() -> None:
    """C1: same broken-photo guard on the detail-page gallery images."""
    html = _render("apt.html", item=FakeView(), similar=[], csrf_token=_CSRF)
    assert "onerror=" in html
    assert "/static/img/placeholder.svg" in html


def test_lead_ok_partial() -> None:
    html = _render("partials/_lead_ok.html")
    assert html.strip()  # non-empty success state


# ---------------------------------------------------------------------------- apt
def test_apt_renders_detail_channel_link_and_note() -> None:
    item = FakeView()
    html = _render("apt.html", item=item, similar=[FakeView(id=200)], csrf_token=_CSRF)
    assert "€" in html and "650" in html
    assert item.channel_url in html  # link to the channel post (never a площадка)
    assert "ВНЖ" in html
    assert "ugovor" in html  # note rendered
    assert "/apt/200" in html  # similar card link
    # address is None per contract — make sure we did not invent one
    assert "None" not in html


def test_apt_rented_badge() -> None:
    html = _render("apt.html", item=FakeView(is_rented=True), similar=[], csrf_token=_CSRF)
    assert "снято" in html


# --- C4: messaging CTA (Telegram only) + share button on the apt page ---


def test_apt_has_no_whatsapp_or_viber_cta() -> None:
    """WhatsApp/Viber were dropped by request — Telegram is the only messaging CTA. The apt page
    must carry NO wa.me / viber:// link and no WhatsApp/Viber labels, while the Telegram CTA stays."""
    html = _render("apt.html", item=FakeView(), similar=[], csrf_token=_CSRF)
    assert "wa.me" not in html
    assert "viber://" not in html
    assert "WhatsApp" not in html
    assert "Viber" not in html
    assert "Открыть в Telegram" in html  # the Telegram CTA remains


def test_apt_share_button_always_renders() -> None:
    """C4: the share button (Web Share API + copy-link fallback) renders regardless of the
    WhatsApp/Viber config — it shares the current page URL, no operator number needed."""
    html = _render("apt.html", item=FakeView(), similar=[], csrf_token=_CSRF)
    assert "data-share" in html  # the share control hook lead.js / share.js wires
    assert "Поделиться" in html


def test_apt_agent_copy_no_longer_contradicts_50pct_commission() -> None:
    """F3 (continuation, DECISIONS-W2): the apt detail page carried two false claims —
    «агент … Комиссию платит хозяин» (reloc item) and «Агент бесплатно — платит хозяин»
    (sticky-CTA bullet). Both contradict the honest 50% подбор-commission. They must be
    reworded to true value-props that do NOT say the service is free or that the landlord
    pays. The «€0» price-side glyph is a separate F-task and is not asserted here."""
    html = _render("apt.html", item=FakeView(), similar=[FakeView(id=200)], csrf_token=_CSRF)
    # the contradicting / false claims are gone
    assert "Комиссию платит хозяин" not in html
    assert "платит хозяин" not in html
    assert "бесплатно" not in html
    assert "бесплат" not in html  # no «бесплатно/бесплатный» anywhere either


def test_no_false_zero_commission_claims_on_apt_and_lead_form() -> None:
    """F3 (final commission-honesty sweep, DECISIONS-W2): nothing on the site may say
    the подбор-commission is €0 / free / nonexistent. Two false claims remained —
    the apt sticky-CTA price suffix «· агент €0», and the lead-form lede sentence
    «Комиссии с тебя нет.». Both must be removed (no invented commission terms in
    their place). Rendered output of BOTH templates must carry neither falsehood."""
    apt = _render("apt.html", item=FakeView(), similar=[FakeView(id=200)], csrf_token=_CSRF)
    lead = _render("partials/_lead_form.html", csrf_token=_CSRF)
    for html in (apt, lead):
        assert "€0" not in html  # no «€0» zero-fee glyph
        assert "агент €" not in html  # no «агент €0» price suffix
        assert "Комиссии с тебя нет" not in html  # no «комиссии … нет» claim


# ---------------------------------------------------------------------------- map
def test_map_renders_container() -> None:
    html = _render("map.html")
    # Leaflet mount point exists; data is pulled client-side in Wave 4
    assert "id=\"map\"" in html or 'id="map"' in html


# --------------------------------------------------------------------- calculator
def test_calculator_renders_shell() -> None:
    html = _render("calculator.html")
    assert html.strip()
    assert "€" in html  # static shell shows currency


# ----------------------------------------------------------- F2 district dropdown
# Curated Cyrillic display superset (a subset of data/districts.yaml's Cyrillic keys),
# alphabetically sorted — mirrors what app.py registers as the `district_options` global.
_DISTRICT_OPTIONS = ["Врачар", "Дорћол", "Звездара", "Земун", "Нови Београд"]


def test_app_registers_district_options_global() -> None:
    """F2: importing src.site.app must register a Jinja global `district_options`
    on the shared `templates` env — a non-empty, alphabetically-sorted list of
    Cyrillic display names derived from data/districts.yaml (works even with no facets)."""
    import src.site.app  # noqa: F401 — import side-effect registers the global
    from src.site.routes import templates

    opts = templates.env.globals.get("district_options")
    assert isinstance(opts, (list, tuple))
    assert len(opts) > 0
    assert "Врачар" in opts  # a known curated Cyrillic district
    assert "Звездара" in opts
    assert list(opts) == sorted(opts)  # alphabetical
    assert len(set(opts)) == len(opts)  # de-duplicated


def test_index_hero_district_is_native_select_from_options() -> None:
    """F2: the hero searchbar район segment is a native <select name="district"> whose
    options come from `district_options` (no-JS fallback; combobox.js enhances it).
    An empty «любой район» default option is present so submit without choosing works."""
    html = _render(
        "index.html",
        featured=[FakeView()],
        stats=_STATS,
        csrf_token=_CSRF,
        district_options=_DISTRICT_OPTIONS,
    )
    # native <select name="district"> exists (no-JS degradation)
    assert re.search(r'<select[^>]*\bname="district"', html)
    # the old free-text input is gone (район is now a <select>, not <input>)
    assert not re.search(r'<input[^>]*\bname="district"', html)
    # options rendered from district_options
    assert ">Врачар<" in html
    assert ">Дорћол<" in html
    # empty default option («любой район») — value="" so it submits as no filter
    assert 'value=""' in html
    assert "любой район" in html


def test_filters_district_options_with_counts() -> None:
    """F2: the filter rail district control offers a native multi-select fallback over the
    same options; when facets.districts has counts, the counts render next to the names."""
    html = _render(
        "partials/_filters.html",
        facets=_FACETS,
        applied=_APPLIED,
        district_options=_DISTRICT_OPTIONS,
    )
    # native multi-select control exists for no-JS deep-linkable multi-select
    assert re.search(r'<select[^>]*\bname="district"[^>]*\bmultiple', html) or re.search(
        r'<select[^>]*\bmultiple[^>]*\bname="district"', html
    )
    assert "Врачар" in html  # district name
    assert "(7)" in html  # count from facets.districts for Врачар
    # applied district preselected (deep-link round-trip)
    assert re.search(r'<option[^>]*value="Врачар"[^>]*selected', html)


def test_filters_district_falls_back_to_options_when_facets_empty() -> None:
    """F2: with no facets (empty catalog), the district filter falls back to the full
    `district_options` superset so the control is never empty."""
    empty_facets = {
        "districts": [],
        "price_bounds": (0, 0),
        "rooms": [],
        "counts": {"vnj": 0, "white_card": 0, "total": 0},
    }
    applied_empty = {**_APPLIED, "district": []}
    html = _render(
        "partials/_filters.html",
        facets=empty_facets,
        applied=applied_empty,
        district_options=_DISTRICT_OPTIONS,
    )
    # still a native <select name="district"> with the curated options
    assert re.search(r'<select[^>]*\bname="district"', html)
    assert "Врачар" in html
    assert "Звездара" in html


# --------------------------------------------------- lead form (index) full check
@pytest.mark.parametrize("template", ["index.html", "apt.html"])
def test_lead_form_has_honeypot_and_csrf(template: str) -> None:
    """The lead form (index and apt) must carry the honeypot + CSRF token field."""
    ctx: dict[str, object] = {"csrf_token": _CSRF}
    if template == "index.html":
        ctx.update(featured=[FakeView()], stats=_STATS)
    else:
        ctx.update(item=FakeView(), similar=[])
    html = _render(template, **ctx)
    assert 'name="company"' in html  # honeypot, exact name
    assert 'name="name"' in html
    assert 'name="contact"' in html
    assert "csrf_token" in html  # CSRF token field present
