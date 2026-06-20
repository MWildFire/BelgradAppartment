"""Read-only engine guarantee + narrow Lead writer (ADR isolation requirement)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from src.db.models import Lead, Listing
from src.site import db as sitedb


@pytest.mark.asyncio
async def test_readonly_session_can_select(site_db: str) -> None:
    async with sitedb.read_session(database_url=site_db) as s:
        rows = (await s.execute(select(Listing.id).order_by(Listing.id))).scalars().all()
    assert rows == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_readonly_session_rejects_insert(site_db: str) -> None:
    with pytest.raises(OperationalError) as exc:
        async with sitedb.read_session(database_url=site_db) as s:
            await s.execute(
                text("INSERT INTO listings (id, platform, external_id, url, photo_urls,"
                     " photo_phashes, scraped_at, last_seen_at, is_active)"
                     " VALUES (99, 'NEKRETNINE', 'z', 'http://z', '[]', '[]',"
                     " '2026-06-01', '2026-06-01', 1)")
            )
    assert "readonly" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_readonly_session_rejects_update(site_db: str) -> None:
    with pytest.raises(OperationalError):
        async with sitedb.read_session(database_url=site_db) as s:
            await s.execute(text("UPDATE listings SET price_eur = 1 WHERE id = 1"))


@pytest.mark.asyncio
async def test_narrow_writer_can_insert_lead(site_db: str) -> None:
    async with sitedb.writer_session(database_url=site_db) as s:
        s.add(Lead(name="Иван", contact="@ivan", budget_eur=700))
    # Read it back through the read-only engine.
    async with sitedb.read_session(database_url=site_db) as s:
        names = (await s.execute(select(Lead.name))).scalars().all()
    assert names == ["Иван"]
