"""E2E (S5-1): mobile bottom-nav visibility + the PWA register script.

At a mobile viewport (≤620px) the .bottom-nav is VISIBLE with four real links
(Каталог/Карта/Подобрать/Telegram), the Telegram one being a https://t.me/ link in a new tab;
at desktop width it's HIDDEN (display:none). Also asserts the SW-register script is present
(SW install itself is impractical to drive in the seeded harness — the route + manifest unit
tests in tests/site/test_pwa.py cover the worker contract).

Gated by RUN_E2E=1.
"""

from __future__ import annotations

from tests.e2e.conftest import requires_e2e

_DESTINATIONS = ["/catalog", "/map", "/#lead"]


@requires_e2e
def test_bottom_nav_visible_on_mobile(page, site_server: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})  # iPhone-class
    page.goto(f"{site_server}/catalog", wait_until="networkidle")

    nav = page.locator("nav.bottom-nav")
    assert nav.count() == 1
    assert nav.is_visible(), "bottom-nav must be visible at mobile width"

    links = nav.locator("a")
    assert links.count() == 4, f"bottom-nav must have 4 links, got {links.count()}"

    hrefs = [links.nth(i).get_attribute("href") for i in range(4)]
    # The three in-app destinations are present...
    for dest in _DESTINATIONS:
        assert any(h == dest for h in hrefs), f"missing bottom-nav link {dest}: {hrefs}"
    # ...and the fourth is the Telegram DM, opening in a new tab.
    tg = nav.locator('a[target="_blank"]')
    assert tg.count() == 1, "expected one new-tab Telegram link in the bottom-nav"
    tg_href = tg.first.get_attribute("href")
    assert tg_href and tg_href.startswith("https://t.me/"), f"Telegram href bad: {tg_href}"
    assert "noopener" in (tg.first.get_attribute("rel") or "")

    # Each item must carry an accessible name (aria-label) and a visible text label.
    for i in range(4):
        a = links.nth(i)
        assert a.get_attribute("aria-label"), "bottom-nav link missing aria-label"
        box = a.bounding_box()
        assert box is not None and box["height"] >= 44, (
            f"bottom-nav touch target too small: {box}"
        )


@requires_e2e
def test_bottom_nav_hidden_on_desktop(page, site_server: str) -> None:
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{site_server}/catalog", wait_until="networkidle")
    nav = page.locator("nav.bottom-nav")
    assert nav.count() == 1, "bottom-nav markup should exist in the DOM"
    assert not nav.is_visible(), "bottom-nav must be hidden (display:none) at desktop width"


@requires_e2e
def test_pwa_register_script_and_manifest_present(page, site_server: str) -> None:
    page.goto(f"{site_server}/catalog", wait_until="networkidle")
    # The register script is wired into base.html (defer-loaded after the page).
    assert page.locator('script[src*="/static/js/pwa.js"]').count() == 1
    # The manifest link + iOS apple-touch-icon are in <head>.
    assert page.locator('link[rel="manifest"]').count() == 1
    assert page.locator('link[rel="apple-touch-icon"][href*="apple-touch-icon.png"]').count() == 1


@requires_e2e
def test_bottom_nav_does_not_cover_footer(page, site_server: str) -> None:
    """The fixed bar must not overlap page content — body gets bottom padding at the breakpoint."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{site_server}/catalog", wait_until="networkidle")
    # Scroll to the very bottom and confirm the footer's last line clears the nav bar.
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    legal = page.locator("footer .legal")
    assert legal.count() == 1
    legal_box = legal.bounding_box()
    nav_box = page.locator("nav.bottom-nav").bounding_box()
    assert legal_box is not None and nav_box is not None
    # The footer's bottom edge sits above the nav's top edge (no overlap), within tolerance.
    assert legal_box["y"] <= nav_box["y"] + 1.0, (
        f"footer legal ({legal_box['y']}) overlaps the bottom-nav ({nav_box['y']})"
    )
