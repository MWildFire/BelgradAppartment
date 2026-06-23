"""Migration 0009 (C2 — saved-search subscriptions: subscriptions + subscription_matches)."""

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


def test_upgrade_0009_creates_tables_and_defaults(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)

    sub_cols = {r[1] for r in con.execute("PRAGMA table_info(subscriptions)")}
    assert {
        "id",
        "criteria",
        "channel",
        "email",
        "telegram_chat_id",
        "token",
        "is_active",
        "created_at",
    } <= sub_cols

    sm_cols = {r[1] for r in con.execute("PRAGMA table_info(subscription_matches)")}
    assert {"id", "subscription_id", "site_listing_id", "notified_at"} <= sm_cols

    # is_active server_default → True (1) when the column is omitted on insert.
    con.execute(
        "INSERT INTO subscriptions (id, criteria, channel, token, created_at)"
        " VALUES (1, '{}', 'email', 'tok-1', ?)",
        (TS,),
    )
    con.commit()
    row = con.execute("SELECT is_active FROM subscriptions WHERE id=1").fetchone()
    assert row == (1,)

    # token is UNIQUE.
    try:
        con.execute(
            "INSERT INTO subscriptions (id, criteria, channel, token, created_at)"
            " VALUES (2, '{}', 'email', 'tok-1', ?)",
            (TS,),
        )
        con.commit()
        raise AssertionError("expected UNIQUE violation on duplicate token")
    except sqlite3.IntegrityError:
        con.rollback()

    con.close()


def test_subscription_matches_unique_guard(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute(
        "INSERT INTO subscriptions (id, criteria, channel, token, is_active, created_at)"
        " VALUES (1, '{}', 'email', 'tok-1', 1, ?)",
        (TS,),
    )
    con.execute(
        "INSERT INTO subscription_matches (id, subscription_id, site_listing_id, notified_at)"
        " VALUES (1, 1, 42, ?)",
        (TS,),
    )
    con.commit()
    # Re-notifying the SAME (subscription, site_listing) is blocked — the idempotency guard.
    try:
        con.execute(
            "INSERT INTO subscription_matches (id, subscription_id, site_listing_id, notified_at)"
            " VALUES (2, 1, 42, ?)",
            (TS,),
        )
        con.commit()
        raise AssertionError("expected UNIQUE violation on duplicate (sub, listing)")
    except sqlite3.IntegrityError:
        con.rollback()
    con.close()


def test_downgrade_0009_drops_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")

    con = sqlite3.connect(db_path)
    assert (
        con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        == "0008_listing_dup_group"
    )
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "subscriptions" not in tables
    assert "subscription_matches" not in tables
    con.close()


def test_models_match_migration_0009_ddl() -> None:
    """models.py declares the schema 0009 adds (create_all == migrated DDL)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert "subscriptions" in tables
    assert "subscription_matches" in tables
    sub_cols = {c["name"] for c in insp.get_columns("subscriptions")}
    assert {"criteria", "channel", "email", "telegram_chat_id", "token", "is_active"} <= sub_cols
