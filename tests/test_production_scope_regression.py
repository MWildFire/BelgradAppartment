"""Keep the 4235abf production coverage behavior connected to CLI and admin output."""

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader

import src.cli as commands
from src.admin import operational_health as health
from src.config import get_settings
from src.db import get_session
from src.db.models import Platform
from src.scrapers.orchestrator import ScrapeResult


@pytest.mark.parametrize("incremental,trusted,fetched,expected", [
    (False, True, 3, "в базе 3/10 (30.0%), не хватает 7 [неполный обход]"),
    (False, False, 3, "полнота базы не подтверждена"),
    (False, True, 11, "полнота базы не подтверждена"),
    (True, True, 3, None),
])
async def test_nightly_coverage_counts_persisted_rows_only(
    monkeypatch, tmp_path, capsys, incremental, trusted, fetched, expected,
):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(commands, "SCRAPERS", {Platform.NEKRETNINE: object})
    result = ScrapeResult(
        platform=Platform.NEKRETNINE, previewed=10, fetched=fetched,
        site_total=10, denominator_trusted=trusted, crawl_complete=False,
    )
    monkeypatch.setattr(commands, "_run_all", AsyncMock(return_value=[result]))
    monkeypatch.setattr(commands, "_run_nightly_steps", AsyncMock(
        return_value=commands.NightlyStepsResult([], [], []),
    ))
    monkeypatch.setattr(commands, "_build_price_drop_alerts", AsyncMock(return_value=([], None)))
    for name in ("_liveness_nightly_line", "_analytics_nightly_line", "_dupes_report_nightly_line"):
        monkeypatch.setattr(commands, name, AsyncMock(return_value="ok"))
    forbidden = AsyncMock(side_effect=AssertionError("external transport opened"))
    monkeypatch.setattr(commands, "_open_userbot_if_needed", forbidden)
    try:
        await commands._nightly_refresh(1, 3, True, False, incremental, no_notify=True)
    except SystemExit as error:
        assert error.code == 1  # A partial crawl remains degraded; its summary still matters.
    text = capsys.readouterr().out
    if expected is not None:
        assert expected in text
    else:
        assert "в базе " not in text and "полнота базы" not in text
    assert "покрытие 100%" not in text
    forbidden.assert_not_called()


async def test_admin_renders_production_market_coverage(db, monkeypatch, tmp_path):
    now = datetime(2026, 10, 5, 12, tzinfo=UTC)
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(health, "schedules", lambda: {})
    monkeypatch.setattr(health, "read_runs", lambda: [{
        "kind": "full", "finished_at": now.isoformat(),
        "sources": [{"platform": "NEKRETNINE", "processed": 3, "site_total": 10,
                     "denominator_trusted": True, "crawl_complete": False}],
    }])
    async with get_session() as session:
        operations = await health.build_operations(session, now=now)
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "src/admin/templates"), autoescape=True,
    )
    rendered = environment.get_template("health.html").render(
        report={"now": now, "operations": operations, "platforms": [],
                "monitor": {"status": "warn", "last_inbound_at": None},
                "fingerprint": {"present": False}},
        request=SimpleNamespace(url=SimpleNamespace(path="/health"),
                                state=SimpleNamespace(csrf_token="fixture")),
    )
    text = re.sub(r"\s+", " ", rendered)
    assert "Полнота базы по трём площадкам" in text
    assert "3 / 10" in text and "30.0%" in text and "неполный обход" in text
