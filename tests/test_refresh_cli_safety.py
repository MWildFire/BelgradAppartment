"""Critical CLI gates: observable failures and evaluations with no side effects."""

from unittest.mock import AsyncMock

import pytest

import src.cli as commands
from src.scrapers.orchestrator import ScrapeResult


async def test_failed_context_keeps_source_result(monkeypatch):
    from src.db.models import Platform

    class FailedScraper:
        async def __aenter__(self):
            raise RuntimeError("unavailable")

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setitem(commands.SCRAPERS, Platform.NEKRETNINE, FailedScraper)
    results = await commands._run_all([Platform.NEKRETNINE], 1, None, False)
    assert len(results) == 1
    assert results[0].platform == Platform.NEKRETNINE
    assert results[0].errors == 1


async def test_all_item_errors_fail_even_with_previews(monkeypatch, tmp_path):
    import src.jobs.run_history as history

    results = [ScrapeResult(platform=p, previewed=100, errors=100) for p in commands.SCRAPERS]
    monkeypatch.setattr(commands, "_run_all", AsyncMock(return_value=results))
    monkeypatch.setattr(
        commands,
        "_run_nightly_steps",
        AsyncMock(return_value=commands.NightlyStepsResult([], [], [])),
    )
    monkeypatch.setattr(commands, "_build_price_drop_alerts", AsyncMock(return_value=([], None)))
    for name in ("_liveness_nightly_line", "_analytics_nightly_line", "_dupes_report_nightly_line"):
        monkeypatch.setattr(commands, name, AsyncMock(return_value="ok"))
    forbidden = AsyncMock(side_effect=AssertionError("external transport opened"))
    monkeypatch.setattr(commands, "_open_userbot_if_needed", forbidden)
    monkeypatch.setattr(history.get_settings(), "data_dir", tmp_path)
    with pytest.raises(SystemExit) as exc:
        await commands._nightly_refresh(1, 3, True, False, no_notify=True)
    assert exc.value.code == 1
    assert list((tmp_path / "state" / "refresh_runs").glob("*.json"))
    forbidden.assert_not_called()


async def test_dry_run_skips_every_mutating_maintenance_step(monkeypatch):
    from types import SimpleNamespace

    import src.site.liveness as liveness

    forbidden = AsyncMock(side_effect=AssertionError("mutation or delivery"))
    for name in (
        "_run_all",
        "_run_nightly_steps",
        "_open_userbot_if_needed",
        "_analytics_nightly_line",
        "_dupes_report_nightly_line",
    ):
        monkeypatch.setattr(commands, name, forbidden)
    evaluated = AsyncMock(return_value=SimpleNamespace(candidates_confirmed=[]))
    monkeypatch.setattr(liveness, "site_liveness_sweep", evaluated)
    await commands._nightly_refresh(1, 3, False, True, dry_run=True)
    evaluated.assert_awaited_once_with(dry_run=True)
    forbidden.assert_not_called()
