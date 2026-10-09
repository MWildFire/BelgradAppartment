from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
from src.config import get_settings
from src.db.models import Base, Listing, Platform
from src.scrapers.base import ListingDetail, ListingPreview
from src.scrapers.orchestrator import _process_one


@pytest_asyncio.fixture
async def factory():
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


def _snapshot(*, address_raw: str | None = "Njegoševa 12") -> ListingDetail:
    return ListingDetail(
        platform=Platform.NEKRETNINE,
        external_id="990001",
        url="https://www.nekretnine.rs/oglasi/990001/",
        price_eur=875,
        area_m2=74.5,
        rooms=2.5,
        district="Vračar",
        address_raw=address_raw,
        lat=44.805,
        lon=20.47,
        title="Dvosoban stan Njegoševa",
        photo_urls=["https://pic.nekretnine.rs/image/5001/xxl.jpg"],
    )


class _Scraper:
    platform = Platform.NEKRETNINE

    def __init__(self, detail: ListingDetail | None = None) -> None:
        self.fetch_detail = AsyncMock(return_value=detail or _snapshot())


@pytest.mark.asyncio
async def test_active_complete_snapshot_skips_detail_for_new_listing(factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "nekretnine_list_snapshot_mode", "active")
    scraper = _Scraper()
    snapshot = _snapshot()
    preview = ListingPreview(
        platform=Platform.NEKRETNINE,
        external_id=snapshot.external_id,
        url=snapshot.url,
        title=snapshot.title,
        snapshot=snapshot,
    )

    inserted, photos, fetched = await _process_one(
        scraper, preview.url, preview.external_id, download_photos=False, preview=preview
    )

    assert inserted is True
    assert photos == 0
    assert fetched is False
    scraper.fetch_detail.assert_not_awaited()
    async with factory() as session:
        row = (await session.execute(select(Listing))).scalar_one()
        assert row.price_eur == 875
        assert row.address_raw == "Njegoševa 12"


@pytest.mark.asyncio
async def test_active_incomplete_snapshot_falls_back_to_detail(factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "nekretnine_list_snapshot_mode", "active")
    scraper = _Scraper()
    snapshot = _snapshot(address_raw=None)
    preview = ListingPreview(
        platform=Platform.NEKRETNINE,
        external_id=snapshot.external_id,
        url=snapshot.url,
        snapshot=snapshot,
    )

    inserted, _, fetched = await _process_one(
        scraper, preview.url, preview.external_id, download_photos=False, preview=preview
    )

    assert inserted is True
    assert fetched is True
    scraper.fetch_detail.assert_awaited_once()


@pytest.mark.asyncio
async def test_shadow_snapshot_keeps_detail_authoritative(factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "nekretnine_list_snapshot_mode", "shadow")
    detail = _snapshot()
    detail.price_eur = 900
    scraper = _Scraper(detail)
    snapshot = _snapshot()
    preview = ListingPreview(
        platform=Platform.NEKRETNINE,
        external_id=snapshot.external_id,
        url=snapshot.url,
        snapshot=snapshot,
    )

    inserted, _, fetched = await _process_one(
        scraper, preview.url, preview.external_id, download_photos=False, preview=preview
    )

    assert inserted is True
    assert fetched is True
    async with factory() as session:
        row = (await session.execute(select(Listing))).scalar_one()
        assert row.price_eur == 900


@pytest.mark.asyncio
async def test_active_existing_listing_merges_non_null_snapshot_fields(factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "nekretnine_list_snapshot_mode", "active")
    snapshot = _snapshot()
    async with factory() as session:
        session.add(
            Listing(
                platform=Platform.NEKRETNINE,
                external_id=snapshot.external_id,
                url=snapshot.url,
                price_eur=700,
                address_raw="Old address",
                photo_urls=snapshot.photo_urls,
                photo_phashes=[],
            )
        )
        await session.commit()

    scraper = _Scraper()
    preview = ListingPreview(
        platform=Platform.NEKRETNINE,
        external_id=snapshot.external_id,
        url=snapshot.url,
        snapshot=snapshot,
    )
    inserted, photos, fetched = await _process_one(
        scraper, preview.url, preview.external_id, download_photos=False, preview=preview
    )

    assert inserted is False
    assert photos == 0
    assert fetched is False
    scraper.fetch_detail.assert_not_awaited()
    async with factory() as session:
        row = (await session.execute(select(Listing))).scalar_one()
        assert row.price_eur == 875
        assert row.address_raw == "Njegoševa 12"
