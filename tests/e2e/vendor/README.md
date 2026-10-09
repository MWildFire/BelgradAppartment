# Offline browser assets

Leaflet 1.9.4 and Leaflet.markercluster 1.5.3 are the exact dependencies already pinned
by SRI in the application templates. `manifest.json` records their sources and SHA-256;
the browser fixture verifies the files before fulfilling CDN URLs from these local bytes.
Third-party licenses are retained in `LICENSE.leaflet` and `LICENSE.markercluster`.

Tests exercise the actual libraries. Other external requests, including OSM tiles, are
aborted; tests validate map/marker behavior rather than the live tile service. The browser
uses a dead loopback proxy as an additional egress barrier, with loopback bypass for the
synthetic application server. No production templates or dependency versions are changed.
