"""Nightly policy through the real summary, exit code and durable run record."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import src.cli as cli
from src.db.models import Platform
from src.jobs import run_history
from src.scrapers import orchestrator
from src.scrapers.freshness import RefreshCandidate, run_fair_refresh, select_fair
from src.scrapers.proxy_pool import NoProxyAvailableError


@pytest.fixture
def nightly(monkeypatch, tmp_path):
    settings = cli.get_settings()
    for name, value in {
        "data_dir": tmp_path,
        "nightly_heal_limit": 0,
        "nightly_detail_refresh": 1,
        "nightly_verify_links": True,
        "nightly_auto_publish": False,
        "nightly_metrics": False,
        "nightly_prune_embeddings": False,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(cli, "_build_price_drop_alerts", AsyncMock(return_value=([], None)))
    for name in ("_liveness_nightly_line", "_analytics_nightly_line", "_dupes_report_nightly_line"):
        monkeypatch.setattr(cli, name, AsyncMock(return_value="ok"))
    monkeypatch.setattr(
        orchestrator, "rolling_detail_refresh",
        AsyncMock(return_value=orchestrator.RollingRefreshResult(
            selected=0, refreshed=0, errors=0, price_changes=[],
        )),
    )
    monkeypatch.setattr(
        orchestrator, "verify_active_links",
        AsyncMock(return_value=orchestrator.LinkVerifyStats()),
    )
    forbidden = AsyncMock(side_effect=AssertionError("external transport opened"))
    monkeypatch.setattr(cli, "_open_userbot_if_needed", forbidden)
    import src.telegram.delivery as delivery
    monkeypatch.setattr(delivery, "send_text_to_recipients", forbidden)

    async def run(*, fields=None, incremental=False, fails=False):
        fields = fields or {}
        results = []
        for platform in cli.SCRAPERS:
            values = dict(
                platform=platform, full_crawl=not incremental, previewed=100, fetched=100,
                site_total=100, denominator_trusted=True, crawl_complete=True,
                pages_attempted=10, stop_reason="complete",
                observed_ids={str(i) for i in range(100)},
            )
            if platform == Platform.HALOOGLASI:
                values.update(fields)
            results.append(orchestrator.ScrapeResult(**values))
        monkeypatch.setattr(cli, "_run_all", AsyncMock(return_value=results))
        if fails:
            with pytest.raises(SystemExit) as exc:
                await cli._nightly_refresh(10, 3, True, False,
                                           incremental=incremental, no_notify=True)
            assert exc.value.code == 1
        else:
            await cli._nightly_refresh(10, 3, True, False,
                                       incremental=incremental, no_notify=True)
        records = run_history.read_runs(tmp_path / "state" / "refresh_runs")
        assert len(records) == 1 and records[0]["finished_at"]
        assert records[0]["status"] == ("degraded" if fails else "success")
        forbidden.assert_not_called()
        return records[0], results

    return run


@pytest.mark.parametrize("incremental,previewed,total,fails", [
    (False, 69, 100, True),
    (False, 70, 100, False),
    (False, 100, 100, False),
    (True, 20, 2604, True),
])
async def test_partial_walk_coverage_exit_and_saved_status(
    db, nightly, incremental, previewed, total, fails,
):
    record, results = await nightly(
        incremental=incremental, fails=fails,
        fields=dict(previewed=previewed, fetched=previewed, site_total=total,
                    skipped_pages=9, pages_failed=9, crawl_complete=False,
                    observed_ids={str(i) for i in range(previewed)}),
    )
    source = next(r for r in results if r.platform == Platform.HALOOGLASI)
    assert source.outcome == "degraded" and not source.sweep_allowed
    assert any("⚠️ Частичный обход" in line for line in record["summary"])
    assert any("🚨 Низкое покрытие" in line for line in record["summary"]) is (
        not incremental and previewed < 70
    )


@pytest.mark.parametrize("startup,code,fails", [
    (TimeoutError, "TimeoutError", True),
    (PlaywrightTimeoutError, "TimeoutError", True),
    (NoProxyAvailableError, "NoProxyAvailableError", False),
    (None, "source_budget_exhausted", False),
])
async def test_rolling_startup_failure_and_budget_reach_nightly_exit(
    db, nightly, monkeypatch, startup, code, fails,
):
    plan = select_fair([
        RefreshCandidate(1, Platform.HALOOGLASI, "https://example.invalid/1"),
    ], limit=1)

    @asynccontextmanager
    async def context(_platform):
        if startup is not None:
            raise startup("startup failed")
        await asyncio.Event().wait()  # only the real source budget ends this wait
        yield None

    callback = AsyncMock(side_effect=AssertionError("startup never completed"))
    source_events = []
    executed = await run_fair_refresh(
        plan, callback, budget_sec=0.05, item_timeout_sec=1, context_factory=context,
        on_source_error=lambda platform, value: source_events.append((platform, value)),
    )
    assert executed.source_errors == {"HALOOGLASI": code}
    assert source_events == [(Platform.HALOOGLASI, code)]
    assert not executed.items[0].attempted and executed.items[0].evidence.reason == code
    callback.assert_not_called()
    monkeypatch.setattr(
        orchestrator, "rolling_detail_refresh",
        AsyncMock(return_value=orchestrator.RollingRefreshResult(
            selected=1, refreshed=0, errors=0, skipped=1, price_changes=[],
            source_errors=executed.source_errors,
        )),
    )
    record, _ = await nightly(fails=fails)
    line = next(line for line in record["summary"] if "деталей обновлено" in line)
    assert line.startswith("🚨 " if fails else "⚠️ ")
    assert f"HALOOGLASI: {code}" in line


@pytest.mark.parametrize("alarm", [False, True])
async def test_x2_removal_alarm_reaches_nightly_exit(db, nightly, monkeypatch, alarm):
    monkeypatch.setattr(
        orchestrator, "verify_active_links",
        AsyncMock(return_value=orchestrator.LinkVerifyStats(
            checked=10, deactivated=4 if alarm else 1, removal_alarm=alarm,
        )),
    )
    record, _ = await nightly(fails=alarm)
    assert any("🚨 массовое снятие ссылок" in line for line in record["summary"]) is alarm


@pytest.mark.parametrize("cleanup_error", [RuntimeError, TimeoutError, PlaywrightTimeoutError])
async def test_cleanup_failure_after_budget_still_fails_night(
    db, nightly, monkeypatch, cleanup_error,
):
    plan = select_fair([
        RefreshCandidate(1, Platform.HALOOGLASI, "https://example.invalid/1"),
    ], limit=1)

    @asynccontextmanager
    async def context(_platform):
        try:
            yield None
        finally:
            raise cleanup_error("cleanup failed after source cancellation")

    async def callback(_row, _context):
        await asyncio.Event().wait()

    executed = await run_fair_refresh(
        plan, callback, budget_sec=0.05, item_timeout_sec=1, context_factory=context,
    )
    code = cleanup_error.__name__
    assert executed.source_errors == {"HALOOGLASI": code}
    assert executed.items[0].attempted and executed.items[0].evidence.reason == code
    monkeypatch.setattr(
        orchestrator, "rolling_detail_refresh",
        AsyncMock(return_value=orchestrator.RollingRefreshResult(
            selected=1, refreshed=0, errors=1, price_changes=[],
            source_errors=executed.source_errors,
        )),
    )
    record, _ = await nightly(fails=True)
    line = next(line for line in record["summary"] if "деталей обновлено" in line)
    assert line.startswith("🚨 ") and f"HALOOGLASI: {code}" in line
