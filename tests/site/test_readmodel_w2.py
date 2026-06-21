"""Stage W2 read-model additions: featured_one + apartment_summary.

Uses the file-backed `site_db` fixture. We toggle is_featured / is_public via a direct
write to the temp DB (the fixture seeds 3 public + 1 private listing, none featured).
"""

from __future__ import annotations

import sqlite3

import pytest

from src.site import db as sitedb
from src.site import readmodel


def _sqlite_path(url: str) -> str:
    # "sqlite+aiosqlite:///<abs_path>" -> "<abs_path>"
    return url.split(":///", 1)[1]


def _set(url: str, sql: str, *params: object) -> None:
    con = sqlite3.connect(_sqlite_path(url))
    con.execute(sql, params)
    con.commit()
    con.close()


@pytest.mark.asyncio
async def test_featured_one_prefers_featured_over_newest(site_db: str) -> None:
    # Newest public (by posted_dt) is id=1 (its PublishedPost.published_at = day 10).
    # Mark id=2 (NOT the newest) as featured -> featured must outrank recency.
    _set(site_db, "UPDATE site_listings SET is_featured=1 WHERE id=2")
    await sitedb.dispose_engines()  # drop cached engine so the UPDATE is visible

    view = await readmodel.featured_one(database_url=site_db)
    assert view is not None
    assert view.id == 2


@pytest.mark.asyncio
async def test_featured_one_falls_back_to_newest_public(site_db: str) -> None:
    # No featured rows -> newest public wins, by posted_dt (same notion as the catalog).
    # id=1 has a PublishedPost.published_at = day 10 (the latest); ids 2/3 use created_at
    # (days 6/7); id=4 is private (excluded). So newest public is id=1.
    view = await readmodel.featured_one(database_url=site_db)
    assert view is not None
    assert view.id == 1


@pytest.mark.asyncio
async def test_featured_one_none_when_no_public(site_db: str) -> None:
    _set(site_db, "UPDATE site_listings SET is_public=0")
    await sitedb.dispose_engines()

    assert await readmodel.featured_one(database_url=site_db) is None


@pytest.mark.asyncio
async def test_apartment_summary_public_returns_dict(site_db: str) -> None:
    summary = await readmodel.apartment_summary(1, database_url=site_db)
    assert summary is not None
    assert summary["id"] == 1
    assert summary["price_eur"] == 600
    assert summary["district"]  # human district present
    assert summary["area_m2"] == 45.0
    assert summary["url"] == "/apt/1"


@pytest.mark.asyncio
async def test_apartment_summary_private_returns_none(site_db: str) -> None:
    # id=4 is is_public=False in the seed.
    assert await readmodel.apartment_summary(4, database_url=site_db) is None


@pytest.mark.asyncio
async def test_apartment_summary_missing_returns_none(site_db: str) -> None:
    assert await readmodel.apartment_summary(999, database_url=site_db) is None
