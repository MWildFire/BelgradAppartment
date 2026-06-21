"""E2E for the Stage W2 conversion-polish flows (task W5-1).

Drives the REAL uvicorn + chromium stack (see tests/e2e/conftest.py::site_server) against the
seeded temp DB. Covers the W2 surfaces: the «Ключи от Белграда» hero, the район combobox, the
50% stat, the site-wide «Написать в Telegram» CTA (header/footer/sticky FAB), per-listing leads,
the real «квартира недели» featured card (is_featured=1 on seeded id=1), the new /vnj /how
/sitemap.xml /robots.txt pages + the custom 404, the lead-form success swap, plus light a11y
(combobox is a focusable control, focus-visible rings, prefers-reduced-motion honored).

All gated behind RUN_E2E (requires_e2e) so the default suite skips them fast.
"""

from __future__ import annotations

import re

import pytest

from tests.e2e.conftest import ARTIFACTS_DIR, requires_e2e

# The Дарья DM target (settings.site_telegram_url default; set as the {{ telegram_url }} global).
DARYA_TG = "https://t.me/Darya_kralj"

# Substrings that must NEVER appear in public HTML — source платформы + Telegram media CDN +
# the private-channel deep link. The public channel (t.me/RentBelgrad) and the Дарья DM
# (t.me/Darya_kralj) are the ONLY t.me links allowed.
FORBIDDEN_URL_BITS = (
    "nekretnine.rs",
    "4zida.rs",
    "halooglasi.com",
    "cdn-telegram",
    "t.me/c/",  # private-channel deep link is площадка-equivalent
    "cdn/remote",  # the seed's remote photo_urls must be collapsed to /photos/*
)


def _assert_no_forbidden_urls(html: str) -> None:
    lowered = html.lower()
    leaks = [bit for bit in FORBIDDEN_URL_BITS if bit.lower() in lowered]
    assert not leaks, f"forbidden source/CDN url(s) leaked into page HTML: {leaks}"


# --------------------------------------------------------------------------- F1 / F3 hero copy


@requires_e2e
def test_hero_copy_and_stat(page, site_server: str) -> None:
    """F1: hero is «Ключи от Белграда» (NOT «по-человечески»). F3: stat shows «50%», not «€0»."""
    page.goto(f"{site_server}/", wait_until="networkidle")

    h1 = page.locator(".hero h1").first
    assert "Ключи от" in h1.inner_text()
    assert "Белграда" in h1.inner_text()

    body_text = page.locator("body").inner_text()
    # F1: the old Stage-W hero copy must be gone.
    assert "по-человечески" not in body_text, "stale «по-человечески» hero copy still present"

    # F3: the 50% commission stat renders literally as «50%» (a real number, not «€0»).
    stat_row = page.locator(".stat-row").first
    assert "50%" in stat_row.inner_text(), "home stat «50%» missing"
    assert "€0" not in stat_row.inner_text(), "placeholder «€0» stat leaked"

    _assert_no_forbidden_urls(page.content())


# --------------------------------------------------------------------------- F2 район dropdown


@requires_e2e
def test_district_dropdown_is_select(page, site_server: str) -> None:
    """F2 (a): the район control in the hero search is a real native <select>/dropdown."""
    page.goto(f"{site_server}/", wait_until="networkidle")
    district = page.locator("form.searchbar select[name='district']")
    assert district.count() == 1, "район control is not a native <select>"
    # The curated Cyrillic option list is present (so the dropdown is a real picker, not empty).
    options = district.locator("option")
    labels = [options.nth(i).inner_text().strip() for i in range(options.count())]
    assert "Врачар" in labels, f"expected «Врачар» among district options, got {labels[:6]}…"


@requires_e2e
def test_district_search_navigates_to_catalog_with_param(page, site_server: str) -> None:
    """F2 (b1): pick a district + submit the hero search → lands on /catalog with district=…

    This half passes today: the form serializes the chosen district into the GET query.
    """
    page.goto(f"{site_server}/", wait_until="networkidle")

    # Use the native <select> directly (the no-JS source of truth the combobox enhances).
    page.select_option("form.searchbar select[name='district']", "Врачар")
    page.locator("form.searchbar button[type='submit']").click()

    page.wait_for_url(re.compile(r"/catalog\?.*district=", re.UNICODE))
    assert "district=" in page.url
    # The chosen value round-trips (URL-encoded «Врачар»).
    assert "%D0%92%D1%80%D0%B0%D1%87%D0%B0%D1%80" in page.url or "Врачар" in page.url


@requires_e2e
def test_district_search_returns_filtered_cards(page, site_server: str) -> None:
    """F2 (b2): the realistic hero search (district only) returns the district's listings.

    Regression for the W5-1 empty-param bug (empty rooms=/price_max= used to poison the filter
    and return 0). Врачар has two seeded listings (id=1 active, id=3 rented) → ≥1 card.
    """
    page.goto(f"{site_server}/", wait_until="networkidle")
    page.select_option("form.searchbar select[name='district']", "Врачар")
    page.locator("form.searchbar button[type='submit']").click()
    page.wait_for_url(re.compile(r"/catalog\?.*district=", re.UNICODE))

    page.wait_for_selector("#results .card-l", timeout=5000)
    cards = page.locator("#results .card-l")
    assert cards.count() >= 1, "filtered catalog returned no cards for Врачар"
    for i in range(cards.count()):
        assert "Врачар" in cards.nth(i).inner_text(), "card not in the filtered district"

    _assert_no_forbidden_urls(page.content())


# --------------------------------------------------------------------------- F5 Telegram CTA


def _assert_darya_cta_everywhere(page) -> None:  # type: ignore[no-untyped-def]
    """The Дарья DM (t.me/Darya_kralj) is reachable from header, footer, and the sticky FAB."""
    # Header nav CTA.
    header = page.locator(f"header.nav a.btn.tg[href='{DARYA_TG}']")
    assert header.count() >= 1, "header «Написать в Telegram» CTA missing"
    # Footer CTA.
    footer = page.locator(f"footer a[href='{DARYA_TG}']")
    assert footer.count() >= 1, "footer Telegram CTA missing"
    # Sticky FAB (fixed bottom-right).
    fab = page.locator(f"a.tg-fab[href='{DARYA_TG}']")
    assert fab.count() == 1, "sticky Telegram FAB missing"
    assert "Написать в Telegram" in fab.get_attribute("aria-label")


@requires_e2e
def test_telegram_cta_present_on_all_page_types(page, site_server: str) -> None:
    """F5: header + footer + sticky FAB → t.me/Darya_kralj on /, /catalog, /apt/{id}, and a 404."""
    base = site_server

    # Home.
    page.goto(f"{base}/", wait_until="networkidle")
    _assert_darya_cta_everywhere(page)

    # Catalog.
    page.goto(f"{base}/catalog", wait_until="networkidle")
    _assert_darya_cta_everywhere(page)

    # Apartment detail (id=1 is public).
    page.goto(f"{base}/apt/1", wait_until="networkidle")
    _assert_darya_cta_everywhere(page)

    # Custom 404 page.
    resp = page.goto(f"{base}/this-page-does-not-exist", wait_until="networkidle")
    assert resp is not None and resp.status == 404
    _assert_darya_cta_everywhere(page)


# --------------------------------------------------------------------------- W3-1 per-listing lead


@requires_e2e
def test_apartment_lead_carries_site_listing_id(page, site_server: str) -> None:
    """W3-1: the apt detail CTA «Оставить заявку на эту квартиру» + a lead form bound to the apt.

    The form's hidden site_listing_id field must carry the apartment's id (server-rendered),
    so a lead from this page is tied to THIS apartment.
    """
    page.goto(f"{site_server}/apt/1", wait_until="networkidle")

    cta = page.locator("a[data-lead-cta]")
    assert cta.count() >= 1, "«Оставить заявку на эту квартиру» CTA missing on apt page"
    assert "Оставить заявку на эту квартиру" in cta.first.inner_text()
    assert cta.first.get_attribute("data-site-listing-id") == "1"

    hidden = page.locator("form.lead#lead input[name='site_listing_id']")
    assert hidden.count() == 1, "hidden site_listing_id field missing on the apt lead form"
    assert hidden.first.get_attribute("value") == "1", "lead form not bound to the apartment id"


@requires_e2e
def test_card_cta_routes_to_apartment_lead(page, site_server: str) -> None:
    """W3-1 (card path): a catalog card's CTA deep-links to /apt/{id}#lead for that apartment."""
    page.goto(f"{site_server}/catalog", wait_until="networkidle")
    card = page.locator("#results .card-l").first
    cta = card.locator("a.card-cta")
    assert cta.count() == 1
    href = cta.get_attribute("href")
    assert href is not None and re.match(r"^/apt/\d+#lead$", href), f"card CTA href off: {href}"


# --------------------------------------------------------------------------- W3-2 featured card


@requires_e2e
def test_featured_apartment_in_hero(page, site_server: str) -> None:
    """W3-2: the hero side-card shows the real featured apartment (price + /apt/{id} link).

    The e2e seed sets is_featured=1 on site_listing id=1 (Vracar ВНЖ €600), so the hero
    «квартира недели» card resolves to that apartment — NOT the «скоро здесь» placeholder.
    """
    page.goto(f"{site_server}/", wait_until="networkidle")

    # The featured card is an <a> wrapping /apt/{id} (placeholder variant is a plain <div>).
    hero_card = page.locator(".hero-side a.hero-photo")
    assert hero_card.count() == 1, "featured hero card (a.hero-photo) missing — got placeholder?"
    href = hero_card.get_attribute("href")
    assert href is not None and re.match(r"^/apt/\d+$", href), f"hero card not /apt/N: {href}"

    card_text = hero_card.inner_text()
    assert "€600" in card_text, f"featured price «€600» missing in hero card: {card_text!r}"
    # Placeholder copy must be gone now that a real apartment is featured.
    assert "скоро здесь" not in card_text, "placeholder «скоро здесь» leaked despite featured row"

    _assert_no_forbidden_urls(page.content())

    # Visual snapshot of / with the featured card.
    page.screenshot(path=str(ARTIFACTS_DIR / "home_featured.png"), full_page=True)


# --------------------------------------------------------------------------- W3-3 content pages


@requires_e2e
def test_vnj_and_how_pages_render(page, site_server: str) -> None:
    """W3-3: /vnj and /how return 200 and render their content (/how shows «50%»/«при заселении»)."""
    base = site_server

    resp = page.goto(f"{base}/vnj", wait_until="networkidle")
    assert resp is not None and resp.status == 200
    assert page.locator("main#main").count() == 1
    assert "ВНЖ" in page.locator("body").inner_text()
    _assert_no_forbidden_urls(page.content())

    resp = page.goto(f"{base}/how", wait_until="networkidle")
    assert resp is not None and resp.status == 200
    how_text = page.locator("body").inner_text()
    assert "50%" in how_text, "/how missing the 50% commission figure"
    assert "при заселении" in how_text, "/how missing «при заселении» commission terms"
    _assert_no_forbidden_urls(page.content())


# --------------------------------------------------------------------------- W3-4 sitemap/robots/404


@requires_e2e
def test_sitemap_robots_and_custom_404(page, site_server: str) -> None:
    """W3-4: /sitemap.xml → 200 XML; /robots.txt → 200; bogus path → 404 custom page (TG CTA)."""
    base = site_server

    sm = page.request.get(f"{base}/sitemap.xml")
    assert sm.ok and sm.status == 200
    ctype = (sm.headers.get("content-type") or "").lower()
    assert "xml" in ctype, f"sitemap content-type not XML: {ctype}"
    body = sm.text()
    assert "<urlset" in body and "<loc>" in body, "sitemap missing urlset/loc"
    # Public /apt ids are enumerated; no площадка/CDN url in the sitemap.
    assert "/apt/1" in body
    _assert_no_forbidden_urls(body)

    rb = page.request.get(f"{base}/robots.txt")
    assert rb.ok and rb.status == 200
    rb_ctype = (rb.headers.get("content-type") or "").lower()
    assert "text/plain" in rb_ctype, f"robots content-type not text/plain: {rb_ctype}"
    assert "Sitemap:" in rb.text() and "sitemap.xml" in rb.text()

    # Custom 404: bogus path renders the styled 404 page (status 404 + Telegram CTA).
    resp = page.goto(f"{base}/nope-{re.escape('zzz')}", wait_until="networkidle")
    assert resp is not None and resp.status == 404
    assert page.locator("#notfound").count() == 1, "custom 404 page not rendered"
    assert page.locator(f"a.btn.tg[href='{DARYA_TG}']").count() >= 1, "404 page missing TG CTA"


# --------------------------------------------------------------------------- lead flow (general)


@requires_e2e
def test_general_lead_success_no_leaks(page, site_server: str) -> None:
    """Lead flow: submit the general home form → success swap (SMTP no-ops, lead saved).

    Also asserts NO competitor/площадка/TG-CDN url appears in any rendered page state — only
    the public channel (t.me/RentBelgrad) and the Дарья DM (t.me/Darya_kralj) are allowed.
    """
    page.goto(f"{site_server}/", wait_until="networkidle")
    _assert_no_forbidden_urls(page.content())

    form = page.locator("form.lead#lead")
    assert form.count() == 1

    page.fill("#lead-name", "E2E Заявка")
    page.fill("#lead-contact", "@e2e_w2")
    form.locator("button[type='submit']").click()

    page.wait_for_selector(".lead-ok", timeout=5000)
    assert "заявка отправлена" in page.locator(".lead-ok").inner_text().lower()
    # Original form was swapped out (not merely hidden).
    assert page.locator("form.lead#lead").count() == 0
    # The post-submit page state must also be free of forbidden urls.
    _assert_no_forbidden_urls(page.content())


# --------------------------------------------------------------------------- a11y (light)


@requires_e2e
def test_combobox_keyboard_and_focus_visible(page, site_server: str) -> None:
    """a11y: the район combobox is a focusable, keyboard-reachable control with a focus ring."""
    page.goto(f"{site_server}/", wait_until="networkidle")

    # combobox.js progressively enhances the <select> into an input[role=combobox]; if JS is
    # active it's the accessible surface, otherwise the native <select> is. Accept either.
    combo = page.locator(
        "form.searchbar input[role='combobox'], form.searchbar select[name='district']"
    ).first
    assert combo.count() >= 1, "no focusable район control found"

    # It must be programmatically focusable (a real control, keyboard-operable).
    combo.focus()
    is_focused = combo.evaluate("el => el === document.activeElement")
    assert is_focused, "район control did not accept keyboard focus"

    # A focus-visible affordance exists: site.css defines :focus-visible outlines globally.
    outline = page.evaluate(
        """() => {
            const sel = '[class*="focus"]';  // not used; computed below
            const el = document.activeElement;
            const cs = getComputedStyle(el);
            // outline OR box-shadow is acceptable as a focus ring.
            return {outlineWidth: cs.outlineWidth, outlineStyle: cs.outlineStyle,
                    boxShadow: cs.boxShadow};
        }"""
    )
    # Don't over-assert the exact ring; just that the focused control reports a non-"none"
    # outline-style/width OR a box-shadow (some token themes use box-shadow rings).
    has_ring = (
        outline["outlineStyle"] not in ("none", "")
        or outline["outlineWidth"] not in ("0px", "")
        or outline["boxShadow"] not in ("none", "")
    )
    assert has_ring, f"focused район control has no visible focus ring: {outline}"


@requires_e2e
def test_reduced_motion_honored_on_fab(page, browser, site_server: str) -> None:
    """a11y: with prefers-reduced-motion, the sticky FAB has no transition (motion suppressed).

    site.css gates the FAB hover/enter motion behind a (prefers-reduced-motion:no-preference)
    or a (reduce) override; under emulated reduce, the computed transition must collapse to none.
    """
    context = browser.new_context(reduced_motion="reduce")
    pg = context.new_page()
    try:
        pg.goto(f"{site_server}/", wait_until="networkidle")
        fab = pg.locator("a.tg-fab")
        assert fab.count() == 1
        transition = fab.evaluate(
            "el => getComputedStyle(el).transitionDuration"
        )
        # Every comma-separated duration must be 0s under reduced motion.
        durations = [d.strip() for d in transition.split(",")]
        assert all(d in ("0s", "0ms", "") for d in durations), (
            f"FAB still animates under prefers-reduced-motion: {transition}"
        )
    finally:
        context.close()


@requires_e2e
def test_hero_combobox_dropdown_is_not_clipped(page, site_server: str) -> None:
    """Regression: the «Район» dropdown must overlay BELOW the searchbar, not be clipped by it.

    The bug was `.searchbar{overflow:hidden}` slicing the absolutely-positioned listbox down to
    the bar's own height — only a ~7px sliver showed, so a район was effectively unpickable.
    Guard: searchbar overflow isn't `hidden`, the open list extends below the bar, and a point
    inside that below-the-bar region resolves to the listbox (i.e. it isn't clipped/behind).
    """
    page.goto(f"{site_server}/", wait_until="networkidle")

    combo = page.locator("form.searchbar input[role='combobox']")
    if combo.count() == 0:
        pytest.skip("combobox.js did not enhance the <select> (no-JS fallback) — clip N/A")

    combo.first.focus()
    page.wait_for_selector("form.searchbar .combobox-list .combobox-option", timeout=4000)

    result = page.evaluate(
        """() => {
            const bar = document.querySelector('form.searchbar');
            const list = document.querySelector('form.searchbar .combobox-list');
            const lr = list.getBoundingClientRect(), sr = bar.getBoundingClientRect();
            const px = lr.left + lr.width / 2;
            const py = sr.bottom + Math.min(40, (lr.bottom - sr.bottom) / 2);
            const hit = document.elementFromPoint(px, py);
            return {
                overflowY: getComputedStyle(bar).overflowY,
                extendsBelowBar: lr.bottom - sr.bottom,
                hitIsList: !!(hit && hit.closest && hit.closest('.combobox-list')),
            };
        }"""
    )
    assert result["overflowY"] != "hidden", "searchbar still clips its dropdown (overflow:hidden)"
    assert result["extendsBelowBar"] > 20, "dropdown is not rendering below the bar"
    assert result["hitIsList"], "dropdown is clipped — a point below the bar isn't the listbox"
