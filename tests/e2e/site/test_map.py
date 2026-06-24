"""E2E: /map boots Leaflet (CDN), fetches /api/map.json, renders ≥1 marker, popup → /apt/{id}.

Two seeded listings carry coords (id=1 ВНЖ, id=3 rented), so the map must show pins. We open a
popup and assert its link targets a /apt/<id> detail page (the only navigation the map offers).
Needs network for jsdelivr (Leaflet) + OSM tiles — gated by RUN_E2E like the rest.
"""

from __future__ import annotations

import re

from tests.e2e.conftest import requires_e2e


@requires_e2e
def test_map(page, site_server: str) -> None:
    base = site_server

    # The data endpoint feeds the map; assert it carries our coord-bearing points first.
    resp = page.request.get(f"{base}/api/map.json")
    assert resp.ok
    points = resp.json()["points"]
    assert len(points) >= 1
    assert all(p["url"].startswith("/apt/") for p in points)

    page.goto(f"{base}/map", wait_until="networkidle")

    # Leaflet attaches .leaflet-container to #map once L.map() runs; markercluster renders
    # either individual price-pins or a cluster bubble. Wait for any of them.
    page.wait_for_selector(".leaflet-container", timeout=10000)
    page.wait_for_function(
        "() => document.querySelectorAll("
        "'.price-pin, .leaflet-marker-icon, .marker-cluster').length >= 1",
        timeout=10000,
    )
    markers = page.locator(".price-pin, .leaflet-marker-icon, .marker-cluster")
    assert markers.count() >= 1, "no Leaflet markers/clusters rendered"

    # Click a marker/cluster to surface a popup; clusters may need an extra click to expand.
    markers.first.click()
    try:
        page.wait_for_selector(".leaflet-popup a[href^='/apt/']", timeout=4000)
    except Exception:
        # If first click hit a cluster, it spiderfies/zooms — click again then re-wait.
        page.locator(".price-pin, .leaflet-marker-icon").first.click()
        page.wait_for_selector(".leaflet-popup a[href^='/apt/']", timeout=6000)

    popup_link = page.locator(".leaflet-popup a[href^='/apt/']").first
    href = popup_link.get_attribute("href")
    assert href is not None and re.match(r"^/apt/\d+$", href), f"popup link not /apt/N: {href}"


@requires_e2e
def test_map_price_buckets_and_vnj_toggle(page, site_server: str) -> None:
    """S5-4: pins carry a price-bucket class (.price-pin.bucket-N) and the ВНЖ toggle filters the
    visible pins to for_vnj-only (re-clustered) and back. Seed: id=1 (600€, ВНЖ) + id=3 (500€,
    non-ВНЖ) both have coords → 2 pins; ВНЖ-on leaves only id=1.
    """
    base = site_server

    # Confirm the seed gives us exactly the points we reason about below (1 ВНЖ + 1 non-ВНЖ).
    points = page.request.get(f"{base}/api/map.json").json()["points"]
    vnj_points = [p for p in points if p["for_vnj"]]
    assert len(points) == 2, f"expected 2 coord points, got {len(points)}"
    assert len(vnj_points) == 1, f"expected 1 ВНЖ point, got {len(vnj_points)}"

    page.goto(f"{base}/map", wait_until="networkidle")
    page.wait_for_selector(".leaflet-container", timeout=10000)

    # Pins render individually (seed coords are far enough apart not to cluster at fit-zoom).
    page.wait_for_function(
        "() => document.querySelectorAll('.price-pin').length >= 2", timeout=10000
    )
    pins = page.locator(".price-pin")
    assert pins.count() == 2, f"expected 2 price pins, got {pins.count()}"

    # Every pin carries a price-bucket class (bucket-1..4) — the colour ramp is applied.
    bucketed = page.locator(
        ".price-pin.bucket-1, .price-pin.bucket-2, .price-pin.bucket-3, .price-pin.bucket-4"
    )
    assert bucketed.count() == 2, "pins missing .bucket-N class"
    # …and the inner span exposes the band via data-bucket (used by the legend/tests).
    assert page.locator(".price-pin .mono[data-bucket]").count() == 2

    # The ВНЖ control is a real, keyboard-operable checkbox.
    toggle = page.locator("#map-vnj-toggle")
    assert toggle.get_attribute("type") == "checkbox"

    # Toggle ON → only the single ВНЖ pin remains (re-clustered to one marker).
    toggle.check()
    page.wait_for_function(
        "() => document.querySelectorAll('.price-pin').length === 1", timeout=6000
    )
    assert page.locator(".price-pin").count() == 1
    assert page.locator(".price-pin.is-vnj").count() == 1, "remaining pin is not the ВНЖ one"

    # Toggle OFF → both pins are back.
    toggle.uncheck()
    page.wait_for_function(
        "() => document.querySelectorAll('.price-pin').length === 2", timeout=6000
    )
    assert page.locator(".price-pin").count() == 2


@requires_e2e
def test_map_renders_base_map_when_empty(page, site_server: str) -> None:
    """Regression: with ZERO points the map must still render Belgrade (not a dead placeholder)
    and surface a non-blocking empty note. The seeded server has points, so we stub
    /api/map.json to an empty set to exercise the empty-catalog path (the live prod state).
    """
    page.route(
        "**/api/map.json",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"points":[]}'
        ),
    )
    page.goto(f"{site_server}/map", wait_until="networkidle")

    # The real Leaflet map mounts even though there are no points.
    page.wait_for_selector(".leaflet-container", timeout=10000)
    # The no-JS placeholder is replaced; a clear empty note points at the list.
    assert page.locator(".map-empty").count() == 0
    note = page.locator(".map-note")
    note.wait_for(timeout=4000)
    assert "каталог" in note.inner_text().lower()
    # No markers/pins when the set is empty.
    assert page.locator(".price-pin, .leaflet-marker-icon").count() == 0
