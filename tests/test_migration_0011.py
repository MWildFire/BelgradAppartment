"""Migration 0011 (liveness pin: site_listings.keep_on_site, D4).

keep_on_site pins a public card so the liveness sweep never auto-hides it. NOT NULL / server_default
0 so every existing card is unpinned after the upgrade; batch-mode rebuild must preserve the rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).parent.parent
TS = "2026-07-10 00:00:00.000000"


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def _seed_site_listing_pre_0011(db_path: Path) -> None:
    """A published card as prod already has it before 0011 — must survive the batch rebuild.

    sqlite3 leaves foreign_keys OFF, so we can insert the row without its match/listing chain.
    """
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO site_listings "
        "(id, match_id, is_public, is_featured, is_for_vnj, is_for_white_card, created_at, updated_at) "
        "VALUES (1, 999, 1, 0, 0, 0, ?, ?)",
        (TS, TS),
    )
    con.commit()
    con.close()


def _cols(db_path: Path) -> dict[str, tuple]:
    con = sqlite3.connect(db_path)
    try:
        return {r[1]: r for r in con.execute("PRAGMA table_info(site_listings)")}
    finally:
        con.close()


def test_upgrade_0011_adds_keep_on_site(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "0010_analytics")  # schema as it stands just before 0011
    _seed_site_listing_pre_0011(db_path)
    command.upgrade(cfg, "head")  # apply 0011

    cols = _cols(db_path)
    assert "keep_on_site" in cols, "0011 must add site_listings.keep_on_site"
    # PRAGMA table_info tuple = (cid, name, type, notnull, dflt_value, pk)
    assert cols["keep_on_site"][3] == 1, "keep_on_site must be NOT NULL"

    con = sqlite3.connect(db_path)
    try:
        val = con.execute("SELECT keep_on_site FROM site_listings WHERE id=1").fetchone()[0]
    finally:
        con.close()
    assert val == 0, "existing cards default to unpinned (server_default 0)"


def test_downgrade_0011_removes_keep_on_site(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0010_analytics")
    assert "keep_on_site" not in _cols(db_path), "downgrade must drop the column"


def test_roundtrip_0011_up_down_up(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0010_analytics")
    command.upgrade(cfg, "head")  # must re-apply cleanly
    assert "keep_on_site" in _cols(db_path)
