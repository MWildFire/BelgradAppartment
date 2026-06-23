"""Migration 0008 (listings.dup_group_id + is_primary for cross-platform dedup, A4)."""

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


def test_upgrade_0008_adds_columns_defaults(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)

    cols = {r[1] for r in con.execute("PRAGMA table_info(listings)")}
    assert "dup_group_id" in cols
    assert "is_primary" in cols

    # Defaults: dup_group_id NULL (ungrouped), is_primary 1 (every listing its own rep).
    con.execute(
        "INSERT INTO listings (id, platform, external_id, url, photo_urls, photo_phashes,"
        " scraped_at, last_seen_at, is_active) VALUES (1, 'NEKRETNINE', 'ext-1', 'http://x',"
        " '[]', '[]', ?, ?, 1)",
        (TS, TS),
    )
    con.commit()
    row = con.execute(
        "SELECT dup_group_id, is_primary FROM listings WHERE id=1"
    ).fetchone()
    assert row == (None, 1)
    con.close()


def test_downgrade_0008_drops_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    # Upgrade to 0008 specifically (not head) so this stays correct as later revisions land.
    command.upgrade(cfg, "0008_listing_dup_group")

    command.downgrade(cfg, "-1")

    con = sqlite3.connect(db_path)
    assert (
        con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        == "0007_featured_lead_fields"
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(listings)")}
    assert "dup_group_id" not in cols
    assert "is_primary" not in cols
    con.close()


def test_models_match_migration_0008_ddl() -> None:
    """models.py declares the schema 0008 adds (create_all == migrated DDL)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("listings")}
    assert "dup_group_id" in cols
    assert "is_primary" in cols
