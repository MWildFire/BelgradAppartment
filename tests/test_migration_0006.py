"""Migration 0006 (site_listings, leads) and the matching model declarations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from src.db.models import Base, Lead, SiteListing

ROOT = Path(__file__).parent.parent
TS = "2026-06-01 00:00:00.000000"


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def _index_sql(con: sqlite3.Connection, name: str) -> str | None:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else None


def _seed_match(con: sqlite3.Connection) -> int:
    """Minimal pipeline rows so site_listings.match_id FK has a target. Returns match id."""
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
    cur.execute(
        "INSERT INTO inbound_posts (id, source_channel_id, tg_message_id, raw_text,"
        " photo_paths, photo_phashes, received_at, status)"
        " VALUES (1, 1, 100, '', '[]', '[]', ?, 'NEW')",
        (TS,),
    )
    cur.execute(
        "INSERT INTO matches (id, inbound_post_id, listing_id, confidence, matched_photo_count,"
        " method, created_at, status) VALUES (1, 1, 1, 0.9, 1, 'phash', ?, 'PENDING')",
        (TS,),
    )
    con.commit()
    return 1


# 0006 is no longer the tip (0007 follows it), so target this revision explicitly
# instead of "head" / "-1" — keeps these tests pinned to the 0006↔0005 boundary.
REV = "0006_site_listings_leads"
PREV = "0005_indexes_dedup_metrics"


def test_upgrade_0006_creates_tables_indexes_defaults(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, REV)

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    tables = {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "site_listings" in tables
    assert "leads" in tables

    # Indexes present.
    assert _index_sql(con, "ix_site_listings_is_public") is not None
    assert _index_sql(con, "ix_leads_created_at") is not None

    match_id = _seed_match(con)

    # site_listings defaults: is_public=1, is_for_vnj=0, is_for_white_card=0.
    con.execute(
        "INSERT INTO site_listings (id, match_id, created_at, updated_at)"
        " VALUES (1, ?, ?, ?)",
        (match_id, TS, TS),
    )
    row = con.execute(
        "SELECT is_public, is_for_vnj, is_for_white_card FROM site_listings WHERE id=1"
    ).fetchone()
    assert row == (1, 0, 0)

    # match_id UNIQUE is enforced.
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO site_listings (id, match_id, created_at, updated_at)"
            " VALUES (2, ?, ?, ?)",
            (match_id, TS, TS),
        )

    # leads defaults: source='site', status='new'.
    con.execute(
        "INSERT INTO leads (id, created_at, name, contact) VALUES (1, ?, 'A', 'tg')",
        (TS,),
    )
    lead_row = con.execute("SELECT source, status FROM leads WHERE id=1").fetchone()
    assert lead_row == ("site", "new")
    con.close()


def test_downgrade_0006_drops_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, REV)

    command.downgrade(cfg, PREV)

    con = sqlite3.connect(db_path)
    assert (
        con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        == PREV
    )
    tables = {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "site_listings" not in tables
    assert "leads" not in tables
    # Indexes gone with their tables.
    assert _index_sql(con, "ix_site_listings_is_public") is None
    assert _index_sql(con, "ix_leads_created_at") is None
    con.close()


def test_models_match_migration_ddl() -> None:
    """models.py declares the same schema 0006 creates (create_all == migrated DDL)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)

    # Subset (not ==): later migrations (0007+) add more columns; assert 0006's are present.
    site_cols = {c["name"] for c in insp.get_columns("site_listings")}
    assert {
        "id", "match_id", "is_public", "is_for_vnj", "is_for_white_card",
        "channel_post_url", "note", "created_at", "updated_at",
    } <= site_cols
    lead_cols = {c["name"] for c in insp.get_columns("leads")}
    assert {
        "id", "created_at", "name", "contact", "budget_eur",
        "districts", "rooms", "message", "source", "status",
    } <= lead_cols
    site_idx = {ix["name"] for ix in insp.get_indexes("site_listings")}
    assert "ix_site_listings_is_public" in site_idx
    lead_idx = {ix["name"] for ix in insp.get_indexes("leads")}
    assert "ix_leads_created_at" in lead_idx

    # match_id UNIQUE declared on the model.
    site_unique = {
        tuple(uc["column_names"]) for uc in insp.get_unique_constraints("site_listings")
    }
    # SQLAlchemy may render unique=True column as a unique index instead of a constraint.
    match_unique = ("match_id",) in site_unique or any(
        ix.get("unique") and ix["column_names"] == ["match_id"]
        for ix in insp.get_indexes("site_listings")
    )
    assert match_unique

    # Build a SiteListing / Lead instance to confirm the ORM mapping is consistent.
    assert SiteListing.__tablename__ == "site_listings"
    assert Lead.__tablename__ == "leads"
