"""E2E harness for the public catalog (Stage W, Wave 5).

Spins up a REAL uvicorn process against a seeded temp FILE sqlite DB and drives it with a
headless chromium (playwright sync_api), so the HTMX swaps, Leaflet map and lead form are
exercised end-to-end — not just the FastAPI handlers.

All e2e are gated behind RUN_E2E=1 (see `requires_e2e`): the default `pytest tests/ -q`
collects them and SKIPS them fast. Nothing here imports playwright at module scope, so a box
without the browser still collects (and skips) cleanly rather than erroring at import.

WHY a subprocess + env (not dependency_overrides like tests/site): the browser talks to a real
server over TCP, so the seam has to be the process environment. DATABASE_URL / SITE_* / an empty
TG_BOT_TOKEN are set in the child env BEFORE it imports the app, and get_settings() is
lru_cache'd per-process, so the child picks them up exactly once.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = Path(__file__).parent / "site" / "_artifacts"


def run_coro(coro) -> None:
    """asyncio.run в worker-треде — НЕ голый asyncio.run(...).

    Если в сессии уже гонялся браузерный e2e, sync-драйвер playwright оставляет в главном
    треде asyncio-loop с флагом «running», и голый asyncio.run() здесь падает RuntimeError-ом
    («cannot be called from a running event loop») — порядок модулей в полном прогоне это
    провоцирует. Worker-тред без loop'а детерминирован при любом порядке тестов.
    Единый хелпер для всех фикстур-сидов (site_server, module-scoped серверы в test_*).
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, coro).result()

# Single source for the RUN_E2E gate — applied per-test so the default suite skips fast.
requires_e2e = pytest.mark.skipif(
    not os.getenv("RUN_E2E"),
    reason="e2e disabled; set RUN_E2E=1 to run (needs chromium + a free TCP port)",
)


# --------------------------------------------------------------------------- seeding
# Replicates tests/site/conftest.py::_seed: ≥3 public listings (one ВНЖ id=1, one rented id=3),
# plus one private (id=4, not public). Photos point at absolute paths OUTSIDE site_photos_root,
# so public_photo_url() collapses them to /photos/<basename> — broken <img> is fine for the flow,
# and crucially NO площадка/CDN URL ever reaches the page.


def _dt(day: int) -> datetime:
    return datetime(2026, 6, day, 12, 0, tzinfo=UTC)


async def _seed(url: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.db.models import (
        Base,
        InboundPost,
        Listing,
        Match,
        MatchStatus,
        Platform,
        PublishedPost,
        SiteListing,
        SourceChannel,
    )

    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(SourceChannel(id=1, tg_id=111))
        await s.flush()

        # id=1 Vracar 2-room 600€ ВНЖ + coords (shows on map).
        s.add(Listing(
            id=1, platform=Platform.NEKRETNINE, external_id="e1", url="http://x/1",
            price_eur=600, area_m2=45.0, rooms=2.0, district="Vracar",
            lat=44.8, lon=20.47, photo_urls=["http://cdn/remote1.jpg"],
        ))
        # id=2 Novi Beograd 1-room 800€ white-card, no coords.
        s.add(Listing(
            id=2, platform=Platform.HALOOGLASI, external_id="e2", url="http://x/2",
            price_eur=800, area_m2=33.0, rooms=1.0, district="Novi Beograd",
            lat=None, lon=None, photo_urls=["http://cdn/remote2.jpg"],
        ))
        # id=3 Vracar studio 500€ rented (is_active False) + coords.
        s.add(Listing(
            id=3, platform=Platform.CETIRI_ZIDA, external_id="e3", url="http://x/3",
            price_eur=500, area_m2=28.0, rooms=0.0, district="Vracar",
            lat=44.81, lon=20.48, is_active=False, link_status="DEAD", photo_urls=["http://cdn/remote3.jpg"],
        ))
        # id=4 Zemun 3-room 1200€ — site_listing NOT public (must never appear).
        s.add(Listing(
            id=4, platform=Platform.NEKRETNINE, external_id="e4", url="http://x/4",
            price_eur=1200, area_m2=70.0, rooms=3.0, district="Zemun",
            lat=44.85, lon=20.4, photo_urls=["http://cdn/remote4.jpg"],
        ))

        for lid in (1, 2, 3, 4):
            s.add(InboundPost(
                id=lid, source_channel_id=1, tg_message_id=100 + lid,
                raw_text="", photo_paths=[f"/abs/data/inbound/1/{lid}/00.jpg"],
            ))
        await s.flush()

        for mid in (1, 2, 3, 4):
            s.add(Match(id=mid, inbound_post_id=mid, listing_id=mid,
                        status=MatchStatus.PUBLISHED))
        await s.flush()

        s.add(PublishedPost(id=1, match_id=1, tg_message_id=999, post_text="x",
                            published_at=_dt(10)))

        # id=1 is also is_featured → drives the «квартира недели» hero side-card (W3-2).
        # It's public + active + has coords + a €600 price, so the featured card renders a
        # concrete apartment (price + /apt/1 link), not the «скоро здесь» placeholder.
        s.add(SiteListing(id=1, match_id=1, is_public=True, is_for_vnj=True,
                          is_for_white_card=False, is_featured=True,
                          created_at=_dt(5), updated_at=_dt(5)))
        s.add(SiteListing(id=2, match_id=2, is_public=True, is_for_vnj=False,
                          is_for_white_card=True, created_at=_dt(6), updated_at=_dt(6)))
        s.add(SiteListing(id=3, match_id=3, is_public=True, is_for_vnj=False,
                          is_for_white_card=False, note="Дарья: торг", created_at=_dt(7),
                          updated_at=_dt(7)))
        s.add(SiteListing(id=4, match_id=4, is_public=False, created_at=_dt(8),
                          updated_at=_dt(8)))
        await s.commit()
    await engine.dispose()


# --------------------------------------------------------------------------- server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_healthz(base_url: str, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:  # server died during boot — fail loudly with its output
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"uvicorn exited early (code {proc.returncode}):\n{out}")
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=1.0) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last_err = e
        time.sleep(0.2)
    raise RuntimeError(f"server not ready within {timeout}s (last error: {last_err})")


@pytest.fixture(scope="session")
def site_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Seeded temp DB + a real uvicorn process. Yields its base_url; torn down at session end."""
    if not os.getenv("RUN_E2E"):
        pytest.skip("e2e disabled; set RUN_E2E=1")

    db_path = tmp_path_factory.mktemp("e2e_db") / "site.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    run_coro(_seed(db_url))

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    # Set BEFORE the child imports the app — get_settings() caches per-process.
    env["DATABASE_URL"] = db_url
    env["SITE_BIND_HOST"] = "127.0.0.1"
    env["SITE_BIND_PORT"] = str(port)
    env["SITE_CHANNEL_URL"] = "https://t.me/RentBelgrad"
    env["SITE_PUBLIC_BASE_URL"] = base_url
    env["TG_BOT_TOKEN"] = ""  # lead notice becomes a safe no-op
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


# --------------------------------------------------------------------------- browser


@pytest.fixture(scope="session")
def browser():  # type: ignore[no-untyped-def]
    """Session-scoped headless chromium. Imported lazily so non-e2e collection never needs it."""
    if not os.getenv("RUN_E2E"):
        pytest.skip("e2e disabled; set RUN_E2E=1")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(browser):  # type: ignore[no-untyped-def]
    """Fresh page per test (own context → isolated localStorage/cookies)."""
    context = browser.new_context()
    pg = context.new_page()
    try:
        yield pg
    finally:
        context.close()


@pytest.fixture(scope="session", autouse=True)
def _artifacts_dir() -> None:
    if os.getenv("RUN_E2E"):
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
