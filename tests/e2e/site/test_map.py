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
