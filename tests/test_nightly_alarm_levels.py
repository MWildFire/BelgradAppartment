"""Nightly summary alarm levels.

Item errors and budget skips are warnings (⚠️) and do not fail the night. Source-level failures,
skipped link checks and failed steps stay alarms (🚨): they mark the run degraded.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.cli as cli_mod
from src.config import get_settings
from src.scrapers import orchestrator


@pytest.fixture
def heal_rolling_links(monkeypatch):
    """Only heal, detail refresh and link verification run; other nightly steps are off."""
    settings = get_settings()
    monkeypatch.setattr(settings, "nightly_heal_limit", 500)
    monkeypatch.setattr(settings, "nightly_detail_refresh", 200)
    monkeypatch.setattr(settings, "nightly_verify_links", True)
    monkeypatch.setattr(settings, "nightly_prune_embeddings", False)
    monkeypatch.setattr(settings, "nightly_metrics", False)

    def use(heal, rolling, links):
        async def fake_heal(*, limit, deadline=None):
            return heal

        async def fake_rolling(*, limit, deadline=None):
            return rolling

        async def fake_links(*, limit, deadline=None):
            return links

        monkeypatch.setattr(orchestrator, "heal_broken_listings", fake_heal)
        monkeypatch.setattr(orchestrator, "rolling_detail_refresh", fake_rolling)
        monkeypatch.setattr(orchestrator, "verify_active_links", fake_links)

    return use


async def _nightly_lines(heal_rolling_links, heal, rolling, links):
    from src.cli import _run_nightly_steps

    heal_rolling_links(heal, rolling, links)
    result = await _run_nightly_steps(notify=False)
    return result.summary_lines


async def test_item_errors_and_budget_skips_are_warnings(db, heal_rolling_links):
    lines = await _nightly_lines(
        heal_rolling_links,
        orchestrator.HealResult(selected=5, fixed=2, remaining=3, errors=4, skipped=3),
        orchestrator.RollingRefreshResult(
            selected=10, refreshed=4, errors=7, price_changes=[], skipped=6,
        ),
        orchestrator.LinkVerifyStats(checked=5, errors=2),
    )

    heal_line = next(line for line in lines if "heal:" in line)
    detail_line = next(line for line in lines if "деталей обновлено" in line)
    link_line = next(line for line in lines if "ссылки: проверено" in line)
    assert heal_line.startswith("⚠️ ") and "ошибок: 4" in heal_line
    assert detail_line.startswith("⚠️ ") and "ошибок: 7" in detail_line
    assert link_line.startswith("⚠️ ") and "ошибок 2" in link_line


async def test_source_failures_and_skipped_link_checks_stay_alarms(db, heal_rolling_links):
    lines = await _nightly_lines(
        heal_rolling_links,
        orchestrator.HealResult(selected=5, fixed=5, remaining=0, errors=0),
        orchestrator.RollingRefreshResult(
            selected=10, refreshed=10, errors=0, price_changes=[],
            source_errors={"NEKRETNINE": "TimeoutError"},
        ),
        orchestrator.LinkVerifyStats(checked=2, skipped=3),
    )

    detail_line = next(line for line in lines if "деталей обновлено" in line)
    link_line = next(line for line in lines if "ссылки: проверено" in line)
    assert detail_line.startswith("🚨 ")
    assert link_line.startswith("🚨 ")


async def test_edge_budget_stop_is_warning_and_other_dupes_failure_stays_alarm(monkeypatch):
    async def edge_budget():
        raise RuntimeError("identity_edge_budget")

    monkeypatch.setattr(cli_mod, "_write_dupes_report", edge_budget)
    budget_line = await cli_mod._dupes_report_nightly_line()
    assert budget_line.startswith("⚠️ ") and "identity_edge_budget" in budget_line
    assert "🚨" not in budget_line

    async def boom():
        raise RuntimeError("dedup exploded")

    monkeypatch.setattr(cli_mod, "_write_dupes_report", boom)
    assert (await cli_mod._dupes_report_nightly_line()).startswith("🚨 ")


def test_item_errors_and_budget_skips_do_not_fail_the_night():
    healthy = dict(
        dead=[], low_coverage=[], degraded=[], parse_degraded=[], sweep_reasons=[],
        maintenance_failed=False,
    )
    assert not cli_mod._nightly_unhealthy(**healthy, results=[SimpleNamespace(outcome="ok", errors=7)])
    assert cli_mod._nightly_unhealthy(
        **{**healthy, "maintenance_failed": True}, results=[SimpleNamespace(outcome="ok", errors=0)],
    )
    assert cli_mod._nightly_unhealthy(
        **healthy, results=[SimpleNamespace(outcome="failed", errors=1)],
    )


def _public_refresh(monkeypatch, **fields):
    async def fake_rolling(*, limit, public_only, scrapers, deadline):
        return SimpleNamespace(selected=10, refreshed=8, price_changes=[], **fields)

    monkeypatch.setattr(orchestrator, "rolling_detail_refresh", fake_rolling)


async def test_public_refresh_item_errors_and_budget_skips_exit_zero(monkeypatch, capsys):
    _public_refresh(monkeypatch, errors=2, skipped=3, source_errors={})
    await cli_mod._refetch_details(None, False, None, False, public_only=True)
    out = capsys.readouterr().out
    assert "⚠️ Ошибки: 2" in out and "⚠️ пропущено по бюджету: 3" in out


async def test_public_refresh_source_failure_exits_one(monkeypatch, capsys):
    _public_refresh(monkeypatch, errors=0, skipped=0, source_errors={"NEKRETNINE": "TimeoutError"})
    with pytest.raises(SystemExit) as exit_info:
        await cli_mod._refetch_details(None, False, None, False, public_only=True)
    assert exit_info.value.code == 1
    assert "🚨 Ошибки площадок: NEKRETNINE: TimeoutError" in capsys.readouterr().out
