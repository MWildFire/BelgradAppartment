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
