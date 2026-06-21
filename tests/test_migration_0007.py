"""Migration 0007 (is_featured + partial index, leads.site_listing_id FK + kind)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from src.db.models import Base

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


def test_upgrade_0007_adds_columns_index_defaults(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")

    # New columns exist.
    site_cols = {r[1] for r in con.execute("PRAGMA table_info(site_listings)")}
    assert "is_featured" in site_cols
    lead_cols = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    assert "site_listing_id" in lead_cols
    assert "kind" in lead_cols

    # Partial index present and scoped WHERE is_featured = 1.
    idx_sql = _index_sql(con, "ix_site_listings_featured")
    assert idx_sql is not None
    assert "is_featured" in idx_sql.lower()
    assert "where" in idx_sql.lower()

    # leads.site_listing_id FK -> site_listings.id is declared.
    fks = list(con.execute("PRAGMA foreign_key_list(leads)"))
    assert any(fk[2] == "site_listings" and fk[3] == "site_listing_id" for fk in fks)

    match_id = _seed_match(con)

    # site_listings.is_featured default = 0.
    con.execute(
        "INSERT INTO site_listings (id, match_id, created_at, updated_at)"
        " VALUES (1, ?, ?, ?)",
        (match_id, TS, TS),
    )
    assert con.execute(
        "SELECT is_featured FROM site_listings WHERE id=1"
    ).fetchone() == (0,)

    # leads.kind default = 'general'.
    con.execute(
        "INSERT INTO leads (id, created_at, name, contact) VALUES (1, ?, 'A', 'tg')",
        (TS,),
    )
    assert con.execute("SELECT kind FROM leads WHERE id=1").fetchone() == ("general",)
    con.close()


def test_downgrade_0007_drops_columns_index(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "-1")

    con = sqlite3.connect(db_path)
    assert (
        con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        == "0006_site_listings_leads"
    )
    site_cols = {r[1] for r in con.execute("PRAGMA table_info(site_listings)")}
    assert "is_featured" not in site_cols
    lead_cols = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    assert "site_listing_id" not in lead_cols
    assert "kind" not in lead_cols
    assert _index_sql(con, "ix_site_listings_featured") is None
    con.close()


def test_models_match_migration_0007_ddl() -> None:
    """models.py declares the schema 0007 adds (create_all == migrated DDL)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)

    site_cols = {c["name"] for c in insp.get_columns("site_listings")}
    assert "is_featured" in site_cols
    lead_cols = {c["name"] for c in insp.get_columns("leads")}
    assert "site_listing_id" in lead_cols
    assert "kind" in lead_cols

    # Partial featured index declared on the model.
    site_idx = {ix["name"] for ix in insp.get_indexes("site_listings")}
    assert "ix_site_listings_featured" in site_idx

    # leads.site_listing_id FK -> site_listings declared on the model.
    lead_fks = insp.get_foreign_keys("leads")
    assert any(fk["referred_table"] == "site_listings" for fk in lead_fks)
