"""Migration 0010 (Stage T analytics: site_events + site_stats_daily + leads UTM columns)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from src.db.models import Base

ROOT = Path(__file__).parent.parent
TS = "2026-07-01 00:00:00.000000"

LEAD_UTM_COLS = {"utm_source", "utm_medium", "utm_campaign", "referrer"}


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def _index_names(con: sqlite3.Connection, table: str) -> set[str]:
    return {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
        )
    }


def _seed_lead_pre_0010(db_path: Path) -> None:
    """A lead as prod would have it before 0010 — survives the batch table rebuild."""
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO leads (id, created_at, name, contact) VALUES (1, ?, 'Анна', '@anna')",
        (TS,),
    )
    con.commit()
    con.close()


def test_upgrade_0010_creates_analytics_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "0010_analytics")  # pin to 0010 — isolate from later migrations (0011+)

    con = sqlite3.connect(db_path)

    ev_cols = {r[1] for r in con.execute("PRAGMA table_info(site_events)")}
    assert {
        "id",
        "ts",
        "type",
        "path",
        "apt_id",
        "visitor",
        "referrer",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "is_bot",
    } <= ev_cols

    # §A.1 indexes: (ts), (type, ts), (apt_id, ts).
    idx = _index_names(con, "site_events")
    assert {
        "ix_site_events_ts",
        "ix_site_events_type_ts",
        "ix_site_events_apt_id_ts",
    } <= idx

    # is_bot defaults to 0 when omitted; apt_id/referrer/utm_* are nullable.
    con.execute(
        "INSERT INTO site_events (id, ts, type, path, visitor) VALUES (1, ?, 'pageview', '/', 'abcd1234abcd1234')",
        (TS,),
    )
    con.commit()
    assert con.execute("SELECT is_bot, apt_id, referrer FROM site_events WHERE id=1").fetchone() == (
        0,
        None,
        None,
    )

    ss_cols = {r[1] for r in con.execute("PRAGMA table_info(site_stats_daily)")}
    assert ss_cols == {"day", "metric", "key", "value"}
    # Composite PK (day, metric, key) — the idempotent-upsert guard.
    con.execute(
        "INSERT INTO site_stats_daily (day, metric, key, value) VALUES ('2026-07-01', 'uniques', '', 5)"
    )
    con.commit()
    try:
        con.execute(
            "INSERT INTO site_stats_daily (day, metric, key, value) VALUES ('2026-07-01', 'uniques', '', 6)"
        )
        con.commit()
        raise AssertionError("expected PK violation on duplicate (day, metric, key)")
    except sqlite3.IntegrityError:
        con.rollback()
    # ...while a different key for the same (day, metric) is a distinct row.
    con.execute(
        "INSERT INTO site_stats_daily (day, metric, key, value) VALUES ('2026-07-01', 'views_apt', '42', 7)"
    )
    con.commit()

    # leads gained the 4 nullable attribution columns.
    lead_info = {r[1]: r for r in con.execute("PRAGMA table_info(leads)")}
    assert LEAD_UTM_COLS <= set(lead_info)
    for col in LEAD_UTM_COLS:
        assert lead_info[col][3] == 0, f"leads.{col} must be nullable"

    con.close()


def test_upgrade_0010_preserves_seeded_leads(tmp_path: Path) -> None:
    """Seeded-DB roundtrip, up leg: existing leads survive the ALTER with NULL utm columns."""
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "0009_subscriptions")
    _seed_lead_pre_0010(db_path)

    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT name, contact, utm_source, utm_medium, utm_campaign, referrer FROM leads WHERE id=1"
    ).fetchone()
    assert row == ("Анна", "@anna", None, None, None, None)
    con.close()


def test_downgrade_0010_reverses_schema_and_keeps_leads(tmp_path: Path) -> None:
    """Seeded-DB roundtrip, down leg: tables dropped, leads columns removed, lead data intact."""
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, "0009_subscriptions")
    _seed_lead_pre_0010(db_path)
    # Pin to 0010 (not "head") so this 0010 roundtrip stays isolated from later migrations (0011+).
    command.upgrade(cfg, "0010_analytics")

    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO site_events (id, ts, type, path, visitor) VALUES (1, ?, 'pageview', '/', 'abcd1234abcd1234')",
        (TS,),
    )
    con.execute("UPDATE leads SET utm_source='tg' WHERE id=1")
    con.commit()
    con.close()

    command.downgrade(cfg, "0009_subscriptions")

    con = sqlite3.connect(db_path)
    assert (
        con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        == "0009_subscriptions"
    )
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "site_events" not in tables
    assert "site_stats_daily" not in tables

    lead_cols = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    assert not (LEAD_UTM_COLS & lead_cols)
    # The pre-0010 lead survived both the up and the down table rebuilds.
    assert con.execute("SELECT name, contact FROM leads WHERE id=1").fetchone() == ("Анна", "@anna")
    con.close()


def test_downgrade_0010_on_empty_db(tmp_path: Path) -> None:
    """Empty-DB roundtrip: 0010 up → down works with zero rows anywhere."""
    db_path = tmp_path / "mig.db"
    cfg = _alembic_cfg(db_path)
    # Absolute revisions (not head/-1) keep this 0010 roundtrip stable as later migrations land.
    command.upgrade(cfg, "0010_analytics")
    command.downgrade(cfg, "0009_subscriptions")

    con = sqlite3.connect(db_path)
    assert (
        con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        == "0009_subscriptions"
    )
    con.close()


def test_models_match_migration_0010_ddl() -> None:
    """models.py declares the schema 0010 adds (create_all == migrated DDL)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert "site_events" in tables
    assert "site_stats_daily" in tables

    ev_cols = {c["name"] for c in insp.get_columns("site_events")}
    assert {"ts", "type", "path", "apt_id", "visitor", "referrer", "is_bot"} <= ev_cols
    ev_idx = {ix["name"] for ix in insp.get_indexes("site_events")}
    assert {"ix_site_events_ts", "ix_site_events_type_ts", "ix_site_events_apt_id_ts"} <= ev_idx

    pk = insp.get_pk_constraint("site_stats_daily")
    assert pk["constrained_columns"] == ["day", "metric", "key"]

    lead_cols = {c["name"] for c in insp.get_columns("leads")}
    assert LEAD_UTM_COLS <= lead_cols
