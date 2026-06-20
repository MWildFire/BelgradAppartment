"""E2E: home → catalog → ВНЖ filter (HTMX swap) → card → detail page.

Asserts the relocant value-prop surfaces (ВНЖ filter narrows results), the detail page shows a
mono €-price + a t.me channel CTA, and — the ToS-critical invariant — that NO площадка / TG-CDN
URL leaks into any rendered HTML.
"""

from __future__ import annotations

import re

from tests.e2e.conftest import ARTIFACTS_DIR, requires_e2e

# Substrings that must NEVER appear in public HTML (source платформы + Telegram media CDN).
FORBIDDEN_URL_BITS = (
    "nekretnine.rs",
    "4zida.rs",
    "halooglasi.com",
    "cdn-telegram",
    "t.me/c/",  # private-channel deep link is площадка-equivalent; the public CTA is t.me/<name>
    "cdn/remote",  # the seed's remote photo_urls — must be filtered to /photos/*
)


def _assert_no_forbidden_urls(html: str) -> None:
    lowered = html.lower()
    leaks = [bit for bit in FORBIDDEN_URL_BITS if bit in lowered]
    assert not leaks, f"forbidden source/CDN url(s) leaked into page HTML: {leaks}"


@requires_e2e
def test_catalog_flow(page, site_server: str) -> None:
    base = site_server

    # 1) Home loads.
    page.goto(f"{base}/", wait_until="networkidle")
    assert "RentBelgrad" in page.title()
    _assert_no_forbidden_urls(page.content())
    page.screenshot(path=str(ARTIFACTS_DIR / "home.png"), full_page=True)

    # 2) Catalog: 3 public listings (private id=4 excluded).
    page.goto(f"{base}/catalog", wait_until="networkidle")
    cards = page.locator("#results .card-l")
    assert cards.count() == 3, f"expected 3 public cards, got {cards.count()}"
    _assert_no_forbidden_urls(page.content())
    page.screenshot(path=str(ARTIFACTS_DIR / "catalog.png"), full_page=True)

    # 3) Toggle ВНЖ → HTMX swaps #results down to the single ВНЖ item (id=1, Врачар €600).
    page.locator("input[name='vnj'][value='1']").check()
    # Wait for the swap: count settles at 1 (HTMX replaces #results in place).
    page.wait_for_function(
        "() => document.querySelectorAll('#results .card-l').length === 1",
        timeout=5000,
    )
    vnj_cards = page.locator("#results .card-l")
    assert vnj_cards.count() == 1
    assert "Врачар" in vnj_cards.first.inner_text()
    assert page.locator("#results .badge.vnj").count() == 1

    # 4) Click the card → /apt/{id}.
    vnj_cards.first.locator("a[href^='/apt/']").first.click()
    page.wait_for_url(re.compile(r"/apt/\d+$"))

    # 5) Price is mono + carries €; channel CTA points at t.me (a channel, not a площадка).
    price = page.locator(".apt-side .price .mono").first
    assert "€" in price.inner_text()
    price_class = page.locator(".apt-side .price .mono").first.get_attribute("class")
    assert price_class is not None and "mono" in price_class

    # Scope to the sticky CTA side — base.html's nav also has an a.btn.ghost (→ /catalog).
    tg_cta = page.locator(".apt-side a.btn.ghost")
    href = tg_cta.first.get_attribute("href")
    assert href is not None and href.startswith("https://t.me/"), f"CTA not a t.me link: {href}"

    _assert_no_forbidden_urls(page.content())
    page.screenshot(path=str(ARTIFACTS_DIR / "apt_detail.png"), full_page=True)


@requires_e2e
def test_rented_still_visible(page, site_server: str) -> None:
    """«снято» is social proof — rented (id=3) stays in the grid, just muted."""
    page.goto(f"{site_server}/catalog", wait_until="networkidle")
    assert page.locator("#results .card-l.is-rented").count() == 1
    assert page.locator("#results .badge.rented").count() == 1
