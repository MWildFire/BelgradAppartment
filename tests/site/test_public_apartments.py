"""F04/F05 regressions: publication rights and availability belong to different scopes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from src.site import readmodel as rm
from src.site.availability import apartment_is_available, apartment_key


def _set(url: str, sql: str) -> None:
    with sqlite3.connect(url.split(":///", 1)[1]) as db:
        db.executescript(sql)


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_apartment_identity_and_activity_do_not_grant_publication_rights() -> None:
    assert apartment_key(1, None) != apartment_key(2, 1)
    assert apartment_key(2, 1) == apartment_key(3, 1)
    assert apartment_is_available([False, True]) is True
    assert apartment_is_available([False, False]) is False
    assert apartment_is_available([]) is False


@pytest.mark.asyncio
async def test_only_secondary_public_is_visible_on_every_surface(site_db: str) -> None:
    _set(site_db, "UPDATE listings SET dup_group_id=4, is_primary=(id=4) WHERE id IN (1,4)")
    views, _ = await rm.list_public(rm.Filters(), database_url=site_db)
    assert {v.id for v in views} == {1, 2}
    assert (await rm.get_public(1, database_url=site_db)).id == 1
    assert await rm.get_public(4, database_url=site_db) is None
    assert (await rm.featured_one(database_url=site_db)).id == 1
    assert 1 in await rm.public_ids(database_url=site_db)
    assert 4 not in await rm.public_ids(database_url=site_db)
    assert {p["id"] for p in await rm.map_points(database_url=site_db)} == {1}
    assert {c.view.id for c in await rm.public_candidates(database_url=site_db)} == {1, 2}


@pytest.mark.asyncio
async def test_public_representative_stays_stable_when_global_primary_changes(site_db: str) -> None:
    _set(site_db, "UPDATE listings SET dup_group_id=1, is_primary=(id=1) WHERE id IN (1,2,4)")
    before, _ = await rm.list_public(rm.Filters(), database_url=site_db)
    _set(site_db, "UPDATE listings SET is_primary=(id=4) WHERE id IN (1,2,4)")
    after, total = await rm.list_public(rm.Filters(), database_url=site_db)
    assert {v.id for v in before} == {1}
    assert {v.id for v in after} == {1}
    assert total == 1
    assert await rm.get_public(4, database_url=site_db) is None


@pytest.mark.asyncio
async def test_secondary_direct_url_keeps_its_own_photos_flags_and_note(site_db: str) -> None:
    _set(site_db, """
        UPDATE listings SET dup_group_id=1, is_primary=(id=1) WHERE id IN (1,2);
        UPDATE site_listings SET note='Дарья: оставить', channel_post_url='https://t.me/example/12' WHERE id=2;
    """)
    secondary = await rm.get_public(2, database_url=site_db)
    assert secondary is not None
    assert secondary.id == 2
    assert secondary.photos == ["/photos/1/2/00.jpg"]
    assert secondary.for_white_card is True
    assert secondary.for_vnj is False
    assert secondary.note == "Дарья: оставить"
    assert secondary.channel_url == "https://t.me/example/12"
    assert (await rm.apartment_summary(2, database_url=site_db))["url"] == "/apt/2"


@pytest.mark.asyncio
async def test_inactive_original_with_private_active_sibling_is_available(site_db: str) -> None:
    _set(site_db, """
        UPDATE listings SET dup_group_id=3, is_primary=(id=4) WHERE id IN (3,4);
        UPDATE site_listings SET is_featured=1 WHERE id=3;
    """)
    detail = await rm.get_public(3, database_url=site_db)
    assert detail is not None and detail.is_rented is False
    views, total = await rm.list_public(rm.Filters(), database_url=site_db)
    assert {v.id for v in views} == {1, 2, 3}
    assert total == 3
    assert (await rm.featured_one(database_url=site_db)).id == 3
    assert {p["id"] for p in await rm.map_points(database_url=site_db)} == {1, 3}
    assert set(await rm.public_ids(database_url=site_db)) == {1, 2, 3}
    candidates = await rm.public_candidates(database_url=site_db)
    assert {c.view.id for c in candidates if rm.listing_matches_filters(c.view, c.rooms_raw, rm.Filters())} == {1, 2, 3}


@pytest.mark.asyncio
async def test_all_inactive_stays_out_of_default_surfaces_but_public_url_survives(site_db: str) -> None:
    _set(site_db, """
        UPDATE listings SET is_active=0, dup_group_id=1, is_primary=(id=1) WHERE id IN (1,2);
        UPDATE site_listings SET is_featured=1 WHERE id=1;
    """)
    assert (await rm.list_public(rm.Filters(), database_url=site_db))[1] == 0
    assert await rm.featured_one(database_url=site_db) is None
    assert await rm.map_points(database_url=site_db) == []
    assert await rm.public_ids(database_url=site_db) == []
    assert await rm.public_candidates(database_url=site_db) == []
    detail = await rm.get_public(2, database_url=site_db)
    assert detail is not None and detail.is_rented is True
    shown, total = await rm.list_public(rm.Filters(show_rented=True), database_url=site_db)
    assert {v.id for v in shown} == {1, 3}
    assert total == 2


@pytest.mark.asyncio
async def test_similar_does_not_suggest_same_apartment_from_secondary_url(site_db: str) -> None:
    _set(site_db, "UPDATE listings SET dup_group_id=1, district='Vracar', is_primary=(id=1) WHERE id IN (1,2)")
    view = await rm.get_public(2, database_url=site_db)
    assert view is not None
    assert await rm.similar(view, database_url=site_db) == []


@pytest.mark.asyncio
async def test_comparison_is_sanitized_and_never_writes(site_db: str) -> None:
    _set(site_db, "UPDATE listings SET dup_group_id=4, is_primary=(id=4) WHERE id IN (1,4)")
    path = site_db.split(":///", 1)[1]
    before_hash = _sha256(path)
    report = await rm.public_selection_comparison(database_url=site_db)
    assert report["no_db_writes"] is True
    assert report["no_telegram_sends"] is True
    assert report["before"]["unique_apartments"] == 1
    assert report["after"]["unique_apartments"] == 2
    assert report["newly_visible"] == [{"site_listing_id": 1, "reason": "public_secondary"}]
    dumped = json.dumps(report)
    assert all(value not in dumped for value in ("http://", "https://", "Vracar", "Нови", "data/inbound"))
    assert _sha256(path) == before_hash
