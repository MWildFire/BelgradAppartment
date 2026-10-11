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


@pytest.mark.parametrize("skipped", [0, 3])
async def test_link_budget_skips_control_nightly_exit_and_durable_status(
    monkeypatch, tmp_path, skipped
):
    import src.jobs.run_history as history
    import src.telegram.bot_delivery as bot_delivery
    import src.telegram.delivery as delivery
    from src.scrapers import orchestrator

    settings = commands.get_settings()
    for name, value in {
        "data_dir": tmp_path,
        "nightly_heal_limit": 0,
        "nightly_detail_refresh": 0,
        "nightly_verify_links": True,
        "nightly_auto_publish": False,
        "nightly_metrics": False,
        "nightly_prune_embeddings": False,
    }.items():
        monkeypatch.setattr(settings, name, value)
    results = [ScrapeResult(platform=p, previewed=5, fetched=5) for p in commands.SCRAPERS]
    monkeypatch.setattr(commands, "_run_all", AsyncMock(return_value=results))
    verify = AsyncMock(
        return_value=orchestrator.LinkVerifyStats(checked=5 - skipped, skipped=skipped)
    )
    monkeypatch.setattr(orchestrator, "verify_active_links", verify)
    monkeypatch.setattr(commands, "_build_price_drop_alerts", AsyncMock(return_value=([], None)))
    for name in ("_liveness_nightly_line", "_analytics_nightly_line", "_dupes_report_nightly_line"):
        monkeypatch.setattr(commands, name, AsyncMock(return_value="ok"))
    forbidden = AsyncMock(side_effect=AssertionError("external transport opened"))
    monkeypatch.setattr(commands, "_open_userbot_if_needed", forbidden)
    monkeypatch.setattr(delivery, "send_text_to_recipients", forbidden)
    monkeypatch.setattr(bot_delivery, "retry_pending_match_deliveries", forbidden)

    if skipped:
        with pytest.raises(SystemExit) as exc:
            await commands._nightly_refresh(5, 3, True, False, incremental=True, no_notify=True)
        assert exc.value.code == 1
    else:
        await commands._nightly_refresh(5, 3, True, False, incremental=True, no_notify=True)

    verify.assert_awaited_once()
    runs = history.read_runs(tmp_path / "state" / "refresh_runs")
    assert len(runs) == 1 and runs[0]["finished_at"]
    assert runs[0]["status"] == ("degraded" if skipped else "success")
    link_line = next(line for line in runs[0]["summary"] if "ссылки: проверено" in line)
    assert link_line.startswith("🚨") is bool(skipped)
    if skipped:
        assert "пропущено по бюджету: 3" in link_line
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
    evaluated = AsyncMock(return_value=SimpleNamespace(candidates_confirmed=[], hidden=[]))
    monkeypatch.setattr(liveness, "site_liveness_sweep", evaluated)
    await commands._nightly_refresh(1, 3, False, True, dry_run=True)
    evaluated.assert_awaited_once_with(dry_run=True)
    forbidden.assert_not_called()


async def test_context_teardown_failure_keeps_actual_partial_counts(monkeypatch):
    from src.db.models import Platform

    class Context:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            raise TimeoutError('teardown')

    async def scrape(scraper, **_options):
        result = ScrapeResult(platform=Platform.NEKRETNINE, previewed=10, fetched=3, updated=3)
        scraper.last_scrape_result = result
        return result

    monkeypatch.setitem(commands.SCRAPERS, Platform.NEKRETNINE, Context)
    monkeypatch.setattr(commands, 'scrape_platform', scrape)
    results = await commands._run_all([Platform.NEKRETNINE], 1, None, False)
    assert results[0].previewed == 10
    assert results[0].fetched == 3
    assert results[0].updated == 3
    assert results[0].outcome == 'degraded'
    assert not results[0].sweep_allowed


@pytest.mark.parametrize("removal_alarm", [False, True])
async def test_removal_alarm_of_the_night_reaches_the_liveness_step(
    monkeypatch, tmp_path, removal_alarm
):
    """Q3.21: the liveness step runs warn-only in a night whose steps raised the removal alarm."""
    import src.jobs.run_history as history

    results = [ScrapeResult(platform=p, previewed=5, fetched=5) for p in commands.SCRAPERS]
    monkeypatch.setattr(commands, "_run_all", AsyncMock(return_value=results))
    steps = commands.NightlyStepsResult([], [], [], 0, removal_alarm)
    monkeypatch.setattr(commands, "_run_nightly_steps", AsyncMock(return_value=steps))
    monkeypatch.setattr(commands, "_build_price_drop_alerts", AsyncMock(return_value=([], None)))
    for name in ("_analytics_nightly_line", "_dupes_report_nightly_line"):
        monkeypatch.setattr(commands, name, AsyncMock(return_value="ok"))
    liveness_line = AsyncMock(return_value="ok")
    monkeypatch.setattr(commands, "_liveness_nightly_line", liveness_line)
    monkeypatch.setattr(history.get_settings(), "data_dir", tmp_path)

    await commands._nightly_refresh(1, 3, True, False, no_notify=True)

    assert liveness_line.await_args.kwargs["auto_hide"] is (not removal_alarm)
