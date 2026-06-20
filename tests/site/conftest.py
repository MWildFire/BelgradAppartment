"""Fixtures for src/site tests: a file-backed SQLite DB seeded with pipeline + overlay rows.

CRITICAL: PRAGMA query_only + a separate read-only engine require a FILE database.
":memory:" gives each connection its own DB, so the writer-seeded rows would be invisible
to the read-only engine and the read-only guarantee could not be exercised.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio
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


def _dt(day: int) -> datetime:
    return datetime(2026, 6, day, 12, 0, tzinfo=UTC)


async def _seed(factory: async_sessionmaker) -> None:
    async with factory() as s:
        chan = SourceChannel(id=1, tg_id=111)
        s.add(chan)
        await s.flush()

        # Listing 1: Vracar, 2 rooms, 600 EUR, has coords + photos, active.
        s.add(
            Listing(
                id=1, platform=Platform.NEKRETNINE, external_id="e1", url="http://x/1",
                price_eur=600, area_m2=45.0, rooms=2.0, district="Vracar",
                lat=44.8, lon=20.47, photo_urls=["http://cdn/remote1.jpg"],
            )
        )
        # Listing 2: Novi Beograd, 1 room, 800 EUR, no coords, active.
        s.add(
            Listing(
                id=2, platform=Platform.HALOOGLASI, external_id="e2", url="http://x/2",
                price_eur=800, area_m2=33.0, rooms=1.0, district="Novi Beograd",
                lat=None, lon=None, photo_urls=["http://cdn/remote2.jpg"],
            )
        )
        # Listing 3: Vracar, studio, 500 EUR, rented (is_active False).
        s.add(
            Listing(
                id=3, platform=Platform.CETIRI_ZIDA, external_id="e3", url="http://x/3",
                price_eur=500, area_m2=28.0, rooms=0.0, district="Vracar",
                lat=44.81, lon=20.48, is_active=False, photo_urls=["http://cdn/remote3.jpg"],
            )
        )
        # Listing 4: Zemun, 3 rooms, 1200 EUR — but its site_listing is NOT public.
        s.add(
            Listing(
                id=4, platform=Platform.NEKRETNINE, external_id="e4", url="http://x/4",
                price_eur=1200, area_m2=70.0, rooms=3.0, district="Zemun",
                lat=44.85, lon=20.4, photo_urls=["http://cdn/remote4.jpg"],
            )
        )

        for lid in (1, 2, 3, 4):
            s.add(
                InboundPost(
                    id=lid, source_channel_id=1, tg_message_id=100 + lid,
                    raw_text="", photo_paths=[f"data/inbound/1/{lid}/00.jpg"],
                )
            )
        await s.flush()

        for mid in (1, 2, 3, 4):
            s.add(
                Match(
                    id=mid, inbound_post_id=mid, listing_id=mid,
                    status=MatchStatus.PUBLISHED,
                )
            )
        await s.flush()

        # published_post only for match 1 -> posted_at comes from published_at.
        s.add(
            PublishedPost(
                id=1, match_id=1, tg_message_id=999, post_text="x", published_at=_dt(10)
            )
        )

        # site_listings overlay.
        s.add(SiteListing(id=1, match_id=1, is_public=True, is_for_vnj=True,
                          is_for_white_card=False, created_at=_dt(5), updated_at=_dt(5)))
        s.add(SiteListing(id=2, match_id=2, is_public=True, is_for_vnj=False,
                          is_for_white_card=True, created_at=_dt(6), updated_at=_dt(6)))
        s.add(SiteListing(id=3, match_id=3, is_public=True, is_for_vnj=False,
                          is_for_white_card=False, note="Дарья: торг", created_at=_dt(7),
                          updated_at=_dt(7)))
        s.add(SiteListing(id=4, match_id=4, is_public=False, created_at=_dt(8),
                          updated_at=_dt(8)))
        await s.commit()


@pytest_asyncio.fixture
async def site_db(tmp_path: Path) -> AsyncIterator[str]:
    """File-backed DB url, schema created, seeded. Yields the sqlite+aiosqlite URL."""
    db_path = tmp_path / "site.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _seed(factory)
    await engine.dispose()
    yield url
