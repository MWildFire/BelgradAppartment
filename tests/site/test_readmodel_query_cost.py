"""Public reads must not hydrate scraper blobs or scan unrelated apartment groups."""

import sqlite3

import pytest

from src.site import readmodel


@pytest.mark.asyncio
async def test_catalog_ignores_unused_scraper_payloads_and_preserves_all_public_fields(site_db):
    path = site_db.split(":///", 1)[1]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE listings SET dup_group_id=1 WHERE id IN (1,4)")
        # The legacy comparison still needs description/title, even though the new
        # public availability comes from source evidence and private sibling activity.
        db.execute("UPDATE listings SET description='Izdato', title='Stan' WHERE id=1")
    before = await readmodel._load_rows(site_db, representatives_only=False)
    comparison = await readmodel.public_selection_comparison(database_url=site_db)
    with sqlite3.connect(path) as db:
        # Invalid unused JSON exposes accidental full-ORM hydration immediately. These
        # fields are pipeline inputs, not inputs to public display or availability.
        db.execute("""UPDATE listings SET raw_payload='invalid unused JSON',
            photo_urls='invalid unused JSON', photo_phashes='invalid unused JSON',
            photo_embeddings=zeroblob(1024*1024)""")
        db.execute("""UPDATE inbound_posts SET photo_phashes='invalid unused JSON',
            photo_embeddings=zeroblob(1024*1024), raw_text='private pipeline text'""")
    after = await readmodel._load_rows(site_db, representatives_only=False)
    assert after == before
    assert await readmodel.public_selection_comparison(database_url=site_db) == comparison
    assert after[0].view.photos == ["/photos/1/1/00.jpg"]
    assert after[0].view.availability == "active"
    assert after[0].legacy_visible is False
    assert {row.view.id for row in after} == {1, 2, 3}


def test_model_created_group_lookup_uses_covering_index(site_db):
    with sqlite3.connect(site_db.split(":///", 1)[1]) as db:
        plan = db.execute("""EXPLAIN QUERY PLAN SELECT dup_group_id,is_active,link_status,
            deactivated_at,absence_streak FROM listings WHERE dup_group_id IN (?,?)""", (1, 2))
        descriptions = [row[3] for row in plan]
    assert any("USING COVERING INDEX ix_listings_group_availability" in row for row in descriptions)
    assert not any("SCAN listings" in row for row in descriptions)
