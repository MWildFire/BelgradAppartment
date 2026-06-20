"""Migration 0005 (indexes, NULL-digest dedup, details_refreshed_at, daily_metrics)
and the matching model declarations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import Base, DailyMetrics, InboundPost, InboundStatus, SourceChannel

ROOT = Path(__file__).parent.parent
TS = "2026-06-01 00:00:00.000000"


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def _seed_pre_0005(db_path: Path) -> None:
    """State as prod could look before 0005: NULL-digest dupes + dependents."""
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO source_channels (id, tg_id, is_active, added_at) VALUES (1, 111, 1, ?)", (TS,)
    )
    cur.execute(
        "INSERT INTO listings (id, platform, external_id, url, photo_urls, photo_phashes,"
        " scraped_at, last_seen_at, is_active) VALUES (1, 'NEKRETNINE', 'ext-1', 'http://x',"
        " '[]', '[]', ?, ?, 1)",
        (TS, TS),
    )
    inbound = [
        # (id, tg_message_id, digest_share_id)
        (1, 100, None),  # keeper: min(id) of the (1, 100, NULL) group
        (2, 100, None),  # dup -> delete (has match + published_post)
        (3, 100, None),  # dup -> delete
        (4, 200, None),  # unique NULL row -> keep
        (5, 300, "share-a"),  # digest rows may share tg_message_id -> keep
        (6, 300, "share-b"),
    ]
    for pk, msg, digest in inbound:
        cur.execute(
            "INSERT INTO inbound_posts (id, source_channel_id, tg_message_id, digest_share_id,"
            " raw_text, photo_paths, photo_phashes, received_at, status)"
            " VALUES (?, 1, ?, ?, '', '[]', '[]', ?, 'NEW')",
            (pk, msg, digest, TS),
        )
    # match 1 hangs off dup row 2 (must be cascaded away), match 2 off keeper row 1.
    for pk, inbound_id in ((1, 2), (2, 1)):
        cur.execute(
            "INSERT INTO matches (id, inbound_post_id, listing_id, confidence,"
            " matched_photo_count, method, created_at, status)"
            " VALUES (?, ?, 1, 0.9, 1, 'phash', ?, 'PENDING')",
            (pk, inbound_id, TS),
        )
    cur.execute(
        "INSERT INTO published_posts (id, match_id, tg_message_id, post_text, published_at)"
        " VALUES (1, 1, 555, 'text', ?)",
        (TS,),
    )
    con.commit()
    con.close()


def _index_sql(con: sqlite3.Connection, name: str) -> str | None:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else None


def test_upgrade_0005_dedup_and_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "0004_digest_share_id")
    _seed_pre_0005(db_path)

    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    # Dedup kept min(id), removed the rest, cascaded dependents.
    assert [r[0] for r in con.execute("SELECT id FROM inbound_posts ORDER BY id")] == [1, 4, 5, 6]
    assert [r[0] for r in con.execute("SELECT id FROM matches")] == [2]
    assert con.execute("SELECT COUNT(*) FROM published_posts").fetchone()[0] == 0

    # Partial unique index present and actually partial.
    sql = _index_sql(con, "uq_inbound_chan_msg_nodigest")
    assert sql is not None and "UNIQUE" in sql.upper() and "digest_share_id IS NULL" in sql

    # Listing indexes + new column.
    assert _index_sql(con, "ix_listings_last_seen_at") is not None
    assert _index_sql(con, "ix_listings_platform_active") is not None
    # Redundant single-column index dropped (left prefix of ix_listings_platform_active).
    assert _index_sql(con, "ix_listing_platform") is None
    cols = [r[1] for r in con.execute("PRAGMA table_info(listings)")]
    assert "details_refreshed_at" in cols

    # daily_metrics table with its unique pair.
    con.execute(
        "INSERT INTO daily_metrics (snapshot_date, platform, active_count, no_photo_count)"
        " VALUES ('2026-06-12', 'NEKRETNINE', 10, 2)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO daily_metrics (snapshot_date, platform, active_count, no_photo_count)"
            " VALUES ('2026-06-12', 'NEKRETNINE', 11, 3)"
        )

    # The race the index exists for: a second NULL-digest row for (chan, msg) is rejected...
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO inbound_posts (source_channel_id, tg_message_id, digest_share_id,"
            " raw_text, photo_paths, photo_phashes, received_at, status)"
            " VALUES (1, 200, NULL, '', '[]', '[]', ?, 'NEW')",
            (TS,),
        )
    # ...while digest shares for the same message stay allowed.
    con.execute(
        "INSERT INTO inbound_posts (source_channel_id, tg_message_id, digest_share_id,"
        " raw_text, photo_paths, photo_phashes, received_at, status)"
        " VALUES (1, 300, 'share-c', '', '[]', '[]', ?, 'NEW')",
        (TS,),
    )
    con.close()


def test_downgrade_0005_reverses_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    # Pin to 0005 (not "head"): later migrations (0006+) add a new head, so "head" + "-1"
    # would no longer land on 0004. This test is specifically about 0005's downgrade.
    command.upgrade(cfg, "0005_indexes_dedup_metrics")

    command.downgrade(cfg, "-1")

    con = sqlite3.connect(db_path)
    assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0004_digest_share_id"
    for name in ("uq_inbound_chan_msg_nodigest", "ix_listings_last_seen_at", "ix_listings_platform_active"):
        assert _index_sql(con, name) is None
    # Symmetric: the single-column platform index dropped in upgrade() is restored.
    assert _index_sql(con, "ix_listing_platform") is not None
    assert "details_refreshed_at" not in [r[1] for r in con.execute("PRAGMA table_info(listings)")]
    assert (
        con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_metrics'").fetchone()
        is None
    )
    con.close()


def test_models_match_migration_ddl() -> None:
    """models.py declares the same schema 0005 creates (create_all == migrated DDL)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)

    listing_indexes = {ix["name"] for ix in insp.get_indexes("listings")}
    assert {"ix_listings_last_seen_at", "ix_listings_platform_active"} <= listing_indexes
    assert "ix_listing_platform" not in listing_indexes
    assert "details_refreshed_at" in {c["name"] for c in insp.get_columns("listings")}
    assert {c["name"] for c in insp.get_columns("daily_metrics")} == {
        "id", "snapshot_date", "platform", "active_count", "no_photo_count",
    }

    # Partial unique from the model enforces the NULL-digest case too.
    with Session(engine) as session:
        chan = SourceChannel(tg_id=111)
        session.add(chan)
        session.flush()
        session.add(
            InboundPost(source_channel_id=chan.id, tg_message_id=100, status=InboundStatus.NEW)
        )
        session.commit()
        session.add(
            InboundPost(source_channel_id=chan.id, tg_message_id=100, status=InboundStatus.NEW)
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(DailyMetrics(snapshot_date="2026-06-12", platform="TOTAL", active_count=1, no_photo_count=0))
        session.commit()
