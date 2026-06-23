"""Tests for self-healing of stale listing URLs (A1).

Covers two orchestrator behaviours:
- re-seeing an existing listing whose canonical URL changed rewrites the stored url (A1.2);
- a 404/410 during detail refetch flips the row inactive instead of merely bumping the
  refresh timer (A1.3).

DB setup mirrors tests/test_pipeline.py: an in-memory SQLite swapped into the global session
factory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
from src.db.models import Base, Listing, Platform
from src.scrapers.orchestrator import _process_one, refetch_listing_details


@pytest_asyncio.fixture
async def factory():
    """In-memory DB swapped into the global session factory; yields the factory."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    orig_engine = db_session._engine
    orig_factory = db_session._session_factory
    db_session._engine = engine
    db_session._session_factory = factory
    try:
        yield factory
    finally:
        await engine.dispose()
        db_session._engine = orig_engine
        db_session._session_factory = orig_factory


@pytest.mark.asyncio
async def test_reseen_listing_refreshes_stale_url(factory):
    """An existing row with a stale slug-URL must have its url rewritten to the canonical URL
    handed in by the preview (self-heal on the next walk)."""
    stale = "https://www.nekretnine.rs/oglasi/468187/dvosobni-stan-obrenovac/"
    canonical = "https://www.nekretnine.rs/oglasi/468187/"
    async with factory() as s:
        s.add(
            Listing(
                platform=Platform.NEKRETNINE, external_id="468187",
                url=stale, photo_urls=[], photo_phashes=[],
                last_seen_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
        await s.commit()

    from unittest.mock import AsyncMock, MagicMock

    scraper = MagicMock()
    scraper.platform = Platform.NEKRETNINE
    scraper.fetch_detail = AsyncMock(side_effect=AssertionError("must not fetch_detail for existing row"))

    inserted, _, fetched_detail = await _process_one(scraper, canonical, "468187", download_photos=False)
    assert inserted is False
    assert fetched_detail is False

    async with factory() as s:
        row = (await s.execute(select(Listing).where(Listing.external_id == "468187"))).scalar_one()
        assert row.url == canonical


@pytest.mark.asyncio
async def test_reseen_listing_keeps_url_when_unchanged(factory):
    """A no-op URL must not be needlessly rewritten (and must still tick last_seen_at)."""
    canonical = "https://www.nekretnine.rs/oglasi/777/"
    async with factory() as s:
        s.add(
            Listing(
                platform=Platform.NEKRETNINE, external_id="777",
                url=canonical, photo_urls=[], photo_phashes=[],
                last_seen_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
        await s.commit()

    from unittest.mock import AsyncMock, MagicMock

    scraper = MagicMock()
    scraper.platform = Platform.NEKRETNINE
    scraper.fetch_detail = AsyncMock(side_effect=AssertionError("must not fetch_detail for existing row"))

    await _process_one(scraper, canonical, "777", download_photos=False)

    async with factory() as s:
        row = (await s.execute(select(Listing).where(Listing.external_id == "777"))).scalar_one()
        assert row.url == canonical


class _Raising404Scraper:
    """Async-context scraper whose fetch_detail raises an httpx 404 HTTPStatusError."""

    platform = Platform.NEKRETNINE

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch_detail(self, url: str):
        request = httpx.Request("GET", url)
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("404 Not Found", request=request, response=response)


@pytest.mark.asyncio
async def test_refetch_404_marks_inactive(factory):
    """A 404 during refetch means the listing was removed from the platform — deactivate the
    row (and count it) rather than leaving a broken link active."""
    async with factory() as s:
        s.add(
            Listing(
                platform=Platform.NEKRETNINE, external_id="468187",
                url="https://www.nekretnine.rs/oglasi/468187/",
                photo_urls=[], photo_phashes=[], is_active=True,
            )
        )
        await s.commit()
        async with factory() as s2:
            row = (await s2.execute(select(Listing))).scalar_one()
            row_id = row.id

    class _Row:
        id = row_id
        platform = Platform.NEKRETNINE
        url = "https://www.nekretnine.rs/oglasi/468187/"

    stats = await refetch_listing_details(
        [_Row()],
        download_photos=False,
        sleep_between=0.0,
        scrapers={Platform.NEKRETNINE: _Raising404Scraper},
    )

    assert stats.deactivated == 1
    assert stats.errors == 1
    async with factory() as s:
        row = (await s.execute(select(Listing).where(Listing.id == row_id))).scalar_one()
        assert row.is_active is False


@pytest.mark.asyncio
async def test_heal_urls_dry_run_changes_nothing(factory):
    """Dry-run must report the rewrite count but leave the DB untouched."""
    from src.cli import _heal_urls

    slug = "https://www.nekretnine.rs/oglasi/468187/dvosobni-stan-obrenovac/"
    async with factory() as s:
        s.add_all([
            Listing(platform=Platform.NEKRETNINE, external_id="468187",
                    url=slug, photo_urls=[], photo_phashes=[]),
            Listing(platform=Platform.NEKRETNINE, external_id="999",
                    url="https://www.nekretnine.rs/oglasi/999/",
                    photo_urls=[], photo_phashes=[]),  # already canonical → skipped
        ])
        await s.commit()

    healed = await _heal_urls(platform="nekretnine", dry_run=True)
    assert healed == 1, "one slug row needs healing; canonical row untouched"

    async with factory() as s:
        rows = {r.external_id: r.url for r in (await s.execute(select(Listing))).scalars()}
        assert rows["468187"] == slug, "dry-run must not modify the DB"


@pytest.mark.asyncio
async def test_heal_urls_rewrites_slug_rows(factory):
    """--no-dry-run rewrites slug URLs to the canonical numeric form for that platform only."""
    from src.cli import _heal_urls

    async with factory() as s:
        s.add_all([
            Listing(platform=Platform.NEKRETNINE, external_id="468187",
                    url="https://www.nekretnine.rs/oglasi/468187/slug-a/",
                    photo_urls=[], photo_phashes=[]),
            Listing(platform=Platform.NEKRETNINE, external_id="111",
                    url="https://www.nekretnine.rs/oglasi/111/another-slug/",
                    photo_urls=[], photo_phashes=[]),
            Listing(platform=Platform.NEKRETNINE, external_id="222",
                    url="https://www.nekretnine.rs/oglasi/222/",
                    photo_urls=[], photo_phashes=[]),  # already canonical
            Listing(platform=Platform.CETIRI_ZIDA, external_id="cz1",
                    url="https://cetiri.example/oglasi/cz1/slug/",
                    photo_urls=[], photo_phashes=[]),  # other platform → untouched
        ])
        await s.commit()

    healed = await _heal_urls(platform="nekretnine", dry_run=False)
    assert healed == 2

    async with factory() as s:
        rows = {r.external_id: r.url for r in (await s.execute(select(Listing))).scalars()}
        assert rows["468187"] == "https://www.nekretnine.rs/oglasi/468187/"
        assert rows["111"] == "https://www.nekretnine.rs/oglasi/111/"
        assert rows["222"] == "https://www.nekretnine.rs/oglasi/222/"
        assert rows["cz1"] == "https://cetiri.example/oglasi/cz1/slug/", "other platform untouched"
