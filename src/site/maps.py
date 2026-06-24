"""Fuzzy external map deep-links for the public catalog (DATA_CONTRACT-S §M, DECISIONS-S D1).

The public site shows only a FUZZY neighbourhood — never the exact building. Both deep-links
open the map *centred on the listing coordinates at zoom 15 with NO pin marker*, so the precise
spot is not revealed.

Coordinate ORDER is the classic bug, pinned here:
  - Yandex `ll=` → LON,LAT
  - Google `@`   → LAT,LON

ToS invariant: these URLs point ONLY at a coordinate area on Yandex/Google — never at a platform
listing or a TG-CDN asset.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.post.location import yandex_maps_url


@dataclass(frozen=True)
class MapsLinks:
    yandex: str  # neighbourhood-centred, NO pt= pin, order LON,LAT
    google: str  # neighbourhood-centred, NO precise pin, order LAT,LON


def google_maps_url(lat: float, lon: float, *, precise: bool = False) -> str:
    """Google Maps URL for a coordinate.

    Order is LAT,LON for Google (opposite of Yandex's LON,LAT).

    - `precise=False` (fuzzy, default): centre the map on the point at zoom 15, no marker:
      ``https://www.google.com/maps/@{lat},{lon},15z``
    - `precise=True` (reserved for future use): drop a search pin on the exact point:
      ``https://www.google.com/maps/search/?api=1&query={lat},{lon}``
    """
    if precise:
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    return f"https://www.google.com/maps/@{lat},{lon},15z"


def maps_links(lat: float | None, lon: float | None) -> MapsLinks | None:
    """Fuzzy Yandex + Google deep-links for a listing, or None if coordinates are missing."""
    if lat is None or lon is None:
        return None
    yandex = yandex_maps_url(lat=lat, lon=lon, precise=False, zoom=15)
    # yandex_maps_url returns a str for the coords-only fuzzy path (both lat/lon present here).
    assert yandex is not None  # noqa: S101 — invariant given non-None coords
    return MapsLinks(yandex=yandex, google=google_maps_url(lat, lon, precise=False))
