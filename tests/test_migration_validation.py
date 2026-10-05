"""Additive validation migrations preserve historical content/delivery and start empty."""

import sqlite3

import pytest

from alembic import command
from tests.test_migration_0011 import _alembic_cfg


def test_validation_migrations_preserve_old_sql_and_delivery_rows(tmp_path):
    database = tmp_path / "legacy.db"
    config = _alembic_cfg(database)
    command.upgrade(config, "0014_group_availability_index")
    with sqlite3.connect(database) as conn:
        conn.execute("""INSERT INTO source_channels(id,tg_id,is_active,added_at)
                        VALUES(1,-12345,1,'2026-10-05')""")
        conn.execute("""INSERT INTO inbound_posts(id,source_channel_id,tg_message_id,photo_paths,
                      photo_phashes,received_at,status) VALUES(1,1,1,'[]','[]','2026-10-05','MATCHED')""")
        conn.execute("""INSERT INTO listings(id,platform,external_id,url,photo_urls,photo_phashes,
                     scraped_at,last_seen_at,is_active,is_primary,absence_streak)
                     VALUES(1,'NEKRETNINE','synthetic','https://example.test','[]','[]',
                            '2026-10-05','2026-10-05',1,1,0)""")
        conn.execute("""INSERT INTO matches(id,inbound_post_id,listing_id,confidence,
                        matched_photo_count,method,created_at,status)
                        VALUES(1,1,1,.99,2,'phash','2026-10-05','PUBLISHED')""")
        conn.execute("""INSERT INTO match_deliveries(id,match_id,recipient_id,status,message_ids,
                        attempts,created_at,updated_at) VALUES(1,1,123,'sent','[17]',1,
                        '2026-10-05','2026-10-05')""")
        before = conn.execute("SELECT * FROM match_deliveries").fetchall()
        old_columns = [r[1] for r in conn.execute("PRAGMA table_info(inbound_posts)")]
        old_inbound = conn.execute("SELECT * FROM inbound_posts").fetchall()
    command.upgrade(config, "head")
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT * FROM match_deliveries").fetchall() == before
        assert (
            conn.execute(f"SELECT {','.join(old_columns)} FROM inbound_posts").fetchall()
            == old_inbound
        )
        assert conn.execute("SELECT photo_evidence FROM inbound_posts").fetchone() == ("[]",)
        for table in (
            "validation_jobs",
            "digest_work_items",
            "listing_identity_edges",
            "apartment_identities",
        ):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(RuntimeError, match="preserve queued work"):
        command.downgrade(config, "0014_group_availability_index")
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT * FROM match_deliveries").fetchall() == before
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0016_identity_edges",
        )
