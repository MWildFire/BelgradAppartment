"""The public-availability index is additive and avoids visiting bulky listing rows."""

import sqlite3

from alembic import command
from tests.test_migration_0011 import _alembic_cfg


def test_group_availability_index_preserves_rows_and_covers_the_lookup(tmp_path):
    database = tmp_path / "before.db"
    config = _alembic_cfg(database)
    command.upgrade(config, "0013_delivery_outcomes")
    query = """SELECT dup_group_id,is_active,link_status,deactivated_at,absence_streak
        FROM listings WHERE dup_group_id IN (?,?)"""
    with sqlite3.connect(database) as db:
        db.executemany("""INSERT INTO listings
            (id,platform,external_id,url,scraped_at,last_seen_at,is_active,photo_urls,photo_phashes,
             is_primary,dup_group_id,link_status,deactivated_at,absence_streak,photo_embeddings)
            VALUES (?,'NEKRETNINE',?,'https://example.invalid','2026-09-08','2026-09-08',?,
                    '[]','[]',1,?,?,?,?,zeroblob(16384))""", [
                        (1, "1", 0, 10, "DEAD", "2026-09-08", 2),
                        (2, "2", 1, 10, "ACTIVE", None, 0),
                        (3, "3", 0, 11, None, None, 0),
                        (4, "4", 1, None, None, None, 0),
                    ])
        before_rows = db.execute("SELECT * FROM listings ORDER BY id").fetchall()
        before_status = db.execute(query, (10, 11)).fetchall()
        assert any("SCAN listings" in row[3] for row in db.execute(
            "EXPLAIN QUERY PLAN " + query, (10, 11),
        ))
    command.upgrade(config, "0014_group_availability_index")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM listings ORDER BY id").fetchall() == before_rows
        assert sorted(db.execute(query, (10, 11)).fetchall()) == sorted(before_status)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query, (10, 11))]
        assert any("USING COVERING INDEX ix_listings_group_availability" in row for row in plan)
        assert not any("SCAN listings" in row for row in plan)
        # An old release can still read its original columns after the additive index.
        assert db.execute("SELECT external_id,is_active FROM listings ORDER BY id").fetchall() == [
            ("1", 0), ("2", 1), ("3", 0), ("4", 1),
        ]
    command.downgrade(config, "0013_delivery_outcomes")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM listings ORDER BY id").fetchall() == before_rows
