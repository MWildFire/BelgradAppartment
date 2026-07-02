"""E2E: точные координаты никогда не покидают БД через публичные поверхности карты (T1-1, §M2).

PERMANENT privacy guard — stays in the suite forever, twin-in-spirit of
test_catalog_flow._assert_no_forbidden_urls. Wave-0 диагностика caught prod /api/map.json
serving listings.lat/lon bit-for-bit; this test seeds a listing with a KNOWN precise pair
(off the 3-decimal grid) and asserts it never appears in /api/map.json, the /map HTML, or
the /apt/{id} HTML (Stage S fuzzy circle — non-regression).

Own server + seed (not the shared `site_server`): the shared seed's coords are already
grid-aligned (44.8 / 20.47) so a leak would be invisible there, and the precise pair has to
land in the DB BEFORE boot because routes.py caches /api/map.json in-process (_cached_map).

No playwright here on purpose: every checked surface is raw server output (the map page
fetches points client-side, so its HTML source IS the surface), and skipping the `browser`
fixture keeps this module runnable in any order without waking the session-scoped chromium.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Iterator

import pytest

from tests.e2e.conftest import (
    REPO_ROOT,
    _free_port,
    _seed,
    _wait_healthz,
    requires_e2e,
    run_coro,
)

# Off-grid pair: fuzz_coords snaps it to (44.812, 20.468) — both digits-visibly different, so a
# plain substring check on any public payload catches a leak of the exact DB value.
PRECISE_LAT = 44.8123456
PRECISE_LON = 20.4676543


def _assert_no_precise_coords(text: str) -> None:
    """Точные координаты из БД не должны встречаться ни в одном публичном ответе."""
    leaks = [c for c in (str(PRECISE_LAT), str(PRECISE_LON)) if c in text]
    assert not leaks, f"precise DB coordinates leaked into a public surface: {leaks}"


async def _reseed_listing1_precise(db_url: str) -> None:
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.db.models import Listing

    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.execute(
            update(Listing)
            .where(Listing.id == 1)
            .values(lat=PRECISE_LAT, lon=PRECISE_LON)
        )
    await engine.dispose()


def _get(base_url: str, path: str) -> str:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as r:
        assert r.status == 200, f"GET {path} → {r.status}"
        return r.read().decode("utf-8")


@pytest.fixture(scope="module")
def privacy_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Standard e2e seed + listing 1 rewritten to the precise pair, own uvicorn process.

    Boot boilerplate mirrors conftest.site_server — duplicated on purpose: the shared fixture
    is session-scoped over a fixed seed and exposes no DB handle (env is set only in the child).
    """
    if not os.getenv("RUN_E2E"):
        pytest.skip("e2e disabled; set RUN_E2E=1")

    db_path = tmp_path_factory.mktemp("e2e_privacy_db") / "site.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    run_coro(_seed(db_url))
    run_coro(_reseed_listing1_precise(db_url))

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["DATABASE_URL"] = db_url
    env["SITE_BIND_HOST"] = "127.0.0.1"
    env["SITE_BIND_PORT"] = str(port)
    env["SITE_CHANNEL_URL"] = "https://t.me/RentBelgrad"
    env["SITE_PUBLIC_BASE_URL"] = base_url
    env["TG_BOT_TOKEN"] = ""
    env["TG_BOT_RECIPIENTS"] = ""
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.site.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_healthz(base_url, proc)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@requires_e2e
def test_map_json_carries_only_snapped_coords(privacy_server: str) -> None:
    from src.site.maps import FUZZ_DECIMALS, fuzz_coords

    body = _get(privacy_server, "/api/map.json")
    _assert_no_precise_coords(body)

    points = json.loads(body)["points"]
    # The precise-seeded listing is still ON the map — privacy must not drop it, only snap it.
    p1 = next(p for p in points if p["id"] == 1)
    assert (p1["lat"], p1["lon"]) == fuzz_coords(PRECISE_LAT, PRECISE_LON)
    # Structural guard beyond the substring check: EVERY serialized point sits on the grid.
    for p in points:
        assert p["lat"] == round(p["lat"], FUZZ_DECIMALS), f"id={p['id']} lat off-grid"
        assert p["lon"] == round(p["lon"], FUZZ_DECIMALS), f"id={p['id']} lon off-grid"


@requires_e2e
def test_map_html_has_no_precise_coords(privacy_server: str) -> None:
    # /map ships no listings server-side (points arrive via /api/map.json, guarded above) —
    # this pins that invariant: the page source itself must stay coordinate-free too.
    _assert_no_precise_coords(_get(privacy_server, "/map"))


@requires_e2e
def test_apt_html_has_no_precise_coords(privacy_server: str) -> None:
    # Stage S non-regression: /apt/1 keeps its fuzzy mini-map — the mount is present and its
    # data-attrs carry the SNAPPED pair, while the precise one stays out of the page source.
    html = _get(privacy_server, "/apt/1")
    assert 'id="apt-map"' in html
    assert f'data-lat="{round(PRECISE_LAT, 3)}"' in html
    assert f'data-lon="{round(PRECISE_LON, 3)}"' in html
    _assert_no_precise_coords(html)
