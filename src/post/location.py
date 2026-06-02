"""Yandex Maps URL builder for the published post (SPEC §6.4).

MVP: long URLs only. Yandex's `/maps/-/...` short codes require a UI action and can't be
generated server-side without an API key. Phase 6 can switch to clck.ru shortener if desired.
"""

from __future__ import annotations

from urllib.parse import quote


def yandex_maps_url(
    *,
    lat: float | None,
    lon: float | None,
    address: str | None = None,
    zoom: int = 17,
    city: str = "Beograd",
) -> str | None:
    """Return a long Yandex Maps URL pointing at the listing.

    Priority:
      1. **Address → text-search.** Yandex resolves the address and *highlights the actual
         street/building*, which is what Дарья needs to see at a glance. Coords (if any) are
         appended as a `ll=...&z=...` centering hint, so the map opens at the right neighbourhood
         even before search resolves.
      2. **Coords-only fallback.** If the listing has no address (rare), pin a point on the map.

    Previously coords-first was used — that produced a red dot in the middle of nowhere because
    Yandex doesn't infer the street from a bare pin.
    """
    if address:
        # Tack on the city so identically-named streets (e.g. "Cara Dušana") resolve to Belgrade,
        # not another Serbian town. Address might already contain it; harmless duplication.
        query = f"{address}, {city}" if city and city.lower() not in address.lower() else address
        url = f"https://yandex.com/maps/?text={quote(query)}"
        if lat is not None and lon is not None:
            url += f"&ll={lon},{lat}&z={zoom}"
        return url
    if lat is not None and lon is not None:
        return (
            f"https://yandex.com/maps/?ll={lon},{lat}&z={zoom}"
            f"&pt={lon},{lat}"
        )
    return None
