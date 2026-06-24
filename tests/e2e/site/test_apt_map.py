"""E2E: /apt/{id} fuzzy mini-map block + Yandex/Google deep-link buttons.

Stage S, Wave 2 (S2-2 / S2-3), DATA_CONTRACT-S §M (FUZZY/D1).

Seed (tests/e2e/conftest.py): id=1 Vracar HAS coords (44.8/20.47); id=2 Novi Beograd has NO
coords. So:
  - /apt/1 → the #apt-map mount is present, Leaflet boots a real .leaflet-container, the FUZZY
    CIRCLE renders (a Leaflet path, NOT an L.marker — no point pin), and 2 deep-link buttons carry
    Yandex/Google coordinate-area hrefs, both target=_blank. No площадка/CDN photo URL leaks.
  - /apt/2 → NO map mount, the «Точный адрес уточняйте» fallback (+ район + Telegram CTA) shows.
Both pages keep the existing t.me CTA + mono €-price (no regression).

Needs network for jsdelivr (Leaflet) + OSM tiles — gated by RUN_E2E like the rest.
"""

from __future__ import annotations

from tests.e2e.conftest import requires_e2e
from tests.e2e.site.test_catalog_flow import _assert_no_forbidden_urls


@requires_e2e
def test_apt_with_coords_has_fuzzy_map_and_deeplinks(page, site_server: str) -> None:
    base = site_server
    page.goto(f"{base}/apt/1", wait_until="networkidle")

    # 1) Map mount present and Leaflet boots a real container (graceful: empty if CDN failed).
    # L.map(el) adds the `leaflet-container` class to the mount element ITSELF (not a child).
    assert page.locator("#apt-map").count() == 1
    page.wait_for_selector("#apt-map.leaflet-container", timeout=10000)

    # 2) FUZZY circle: a Leaflet vector path (circle) renders — and NO point marker (privacy/D1).
    page.wait_for_function(
        "() => document.querySelectorAll("
        "'#apt-map .leaflet-overlay-pane path').length >= 1",
        timeout=10000,
    )
    assert page.locator("#apt-map .leaflet-overlay-pane path").count() >= 1, "no fuzzy circle drawn"
    assert page.locator("#apt-map .leaflet-marker-icon").count() == 0, "a point marker leaked (D1)"

    # 3) Two deep-link buttons → coordinate-area Yandex/Google maps, both new-tab.
    links = page.locator(".apt-map-links a.btn")
    assert links.count() == 2
    hrefs = [links.nth(i).get_attribute("href") for i in range(2)]
    yandex = [h for h in hrefs if h and h.startswith("https://yandex.com/maps/")]
    google = [h for h in hrefs if h and h.startswith("https://www.google.com/maps/")]
    assert len(yandex) == 1, f"no yandex coordinate-area link: {hrefs}"
    assert len(google) == 1, f"no google coordinate-area link: {hrefs}"
    for i in range(2):
        assert links.nth(i).get_attribute("target") == "_blank"
        rel = links.nth(i).get_attribute("rel") or ""
        assert "noopener" in rel

    # 4) ToS invariant: not a single площадка / photo-CDN URL anywhere on the page.
    _assert_no_forbidden_urls(page.content())

    # 5) No regression: existing t.me channel CTA + mono €-price still present.
    tg_cta = page.locator(".apt-side a.btn.ghost")
    href = tg_cta.first.get_attribute("href")
    assert href is not None and href.startswith("https://t.me/"), f"CTA not a t.me link: {href}"
    price = page.locator(".apt-side .price .mono").first
    assert "€" in price.inner_text()


@requires_e2e
def test_apt_without_coords_shows_fallback(page, site_server: str) -> None:
    base = site_server
    page.goto(f"{base}/apt/2", wait_until="networkidle")

    # No coords (seed id=2) → no map mount, no external deep-links.
    assert page.locator("#apt-map").count() == 0
    assert page.locator(".apt-map-links").count() == 0

    # §M fallback: «Точный адрес уточняйте» + район + a Telegram CTA.
    fallback = page.locator(".apt-loc-fallback")
    assert fallback.count() == 1
    text = fallback.inner_text()
    assert "Точный адрес уточняйте" in text
    assert "Нови Београд" in text  # the normalised район (cyrillic)
    tg = fallback.locator("a.btn")
    assert tg.first.get_attribute("href", ).startswith("https://t.me/")

    # Still no площадка/CDN leaks, and the sticky CTA + mono price are intact.
    _assert_no_forbidden_urls(page.content())
    assert page.locator(".apt-side .price .mono").first.inner_text().find("€") >= 0
