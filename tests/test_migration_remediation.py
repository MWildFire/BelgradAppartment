import sqlite3

from alembic import command
from tests.test_migration_0011 import _alembic_cfg


def test_additive_migration_keeps_attempts_and_unknown_success(tmp_path):
    database = tmp_path / "old.db"
    config = _alembic_cfg(database)
    command.upgrade(config, "0011_keep_on_site")
    with sqlite3.connect(database) as connection:
        connection.execute("""INSERT INTO listings
            (id,platform,external_id,url,scraped_at,last_seen_at,is_active,photo_urls,photo_phashes,
             is_primary,details_refreshed_at) VALUES
            (1,'NEKRETNINE','old','https://x/1','2026-09-01','2026-09-01',1,'[]','[]',1,'2026-09-02')""")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        row = connection.execute("""SELECT details_refreshed_at,details_succeeded_at,
            absence_streak,link_status FROM listings WHERE id=1""").fetchone()
        assert row == ("2026-09-02", None, 0, None)
        assert connection.execute("SELECT count(*) FROM match_deliveries").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        # Old-version SQL remains usable after additive migration, without a DB downgrade.
        assert connection.execute("SELECT external_id,is_active FROM listings").fetchone() == (
            "old",
            1,
        )
