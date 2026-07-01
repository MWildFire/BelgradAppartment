"""T1-1 privacy: map_points snaps lat/lon to the ~100m grid BEFORE serialization (§M2).

Диагностика Wave 0 подтвердила утечку: точки /api/map.json повторяли listings.lat/lon
бит-в-бит. Политика приватности та же, что в Stage S (D1/ФУЗЗИ): точные координаты не
покидают БД ни через одну публичную поверхность.

The shared seed's coords (44.8 / 20.47) are ALREADY on the 3-decimal grid, so snapping would
be invisible — each test first rewrites listing 1 to a known PRECISE pair and asserts the
read-model returns only its fuzz_coords() image. The /apt path is pinned as a NON-regression:
get_public() must keep the precise view — routes.py fuzzes at render time (Stage S behaviour).
"""

from __future__ import annotations

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from src.db.models import Listing
from src.site import readmodel as rm
from src.site.maps import FUZZ_DECIMALS, fuzz_coords

# A pair that is NOT on the privacy grid: fuzz_coords → (44.812, 20.468), both != precise.
PRECISE_LAT = 44.8123456
PRECISE_LON = 20.4676543


async def _make_listing1_precise(site_db: str) -> None:
    """Overwrite listing 1's grid-aligned seed coords with the precise pair (writer engine —
    the read-model's own read_session is PRAGMA query_only and cannot do this)."""
    engine = create_async_engine(site_db, future=True)
    async with engine.begin() as conn:
        await conn.execute(
            update(Listing)
            .where(Listing.id == 1)
            .values(lat=PRECISE_LAT, lon=PRECISE_LON)
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_map_points_snaps_coords_before_serialization(site_db: str) -> None:
    await _make_listing1_precise(site_db)
    points = await rm.map_points(database_url=site_db)
    p1 = next(p for p in points if p["id"] == 1)
    # The serialized point is exactly the fuzz_coords image — never the DB value.
    assert (p1["lat"], p1["lon"]) == fuzz_coords(PRECISE_LAT, PRECISE_LON)
    assert p1["lat"] != PRECISE_LAT
    assert p1["lon"] != PRECISE_LON


@pytest.mark.asyncio
async def test_map_points_every_point_is_on_the_grid(site_db: str) -> None:
    await _make_listing1_precise(site_db)
    points = await rm.map_points(database_url=site_db)
    assert points  # the seed guarantees coord-bearing rows — an empty pass would prove nothing
    for p in points:
        lat, lon = p["lat"], p["lon"]
        assert isinstance(lat, float) and isinstance(lon, float)
        # Grid membership: rounding to FUZZ_DECIMALS is a fixpoint for already-snapped values.
        assert lat == round(lat, FUZZ_DECIMALS), f"id={p['id']} lat off-grid: {lat}"
        assert lon == round(lon, FUZZ_DECIMALS), f"id={p['id']} lon off-grid: {lon}"


@pytest.mark.asyncio
async def test_map_points_snap_is_deterministic(site_db: str) -> None:
    # Клиентская кластеризация группирует по этим значениям: два чтения должны дать
    # бит-в-бит одинаковые точки, иначе кластеры «плавали» бы между запросами.
    await _make_listing1_precise(site_db)
    first = await rm.map_points(database_url=site_db)
    second = await rm.map_points(database_url=site_db)
    assert first == second


@pytest.mark.asyncio
async def test_apt_path_get_public_keeps_precise_view(site_db: str) -> None:
    """NON-regression for /apt/{id} (Stage S: 150m circle + fuzzy deep-links): the VIEW stays
    precise — routes.py applies fuzz_coords at render. Fuzzing here too would be harmless
    (round is idempotent) but would silently change the seam the route tests pin."""
    await _make_listing1_precise(site_db)
    view = await rm.get_public(1, database_url=site_db)
    assert view is not None
    assert view.lat == PRECISE_LAT
    assert view.lon == PRECISE_LON
