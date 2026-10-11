"""Nightly summary alarm levels.

Item errors below half of the items a source processed, and budget skips, are warnings (⚠️).
At least half of them make the source degraded, and a degraded source fails the night (PR #6,
owner decision Q3.16). Source-level failures, skipped link checks and failed steps stay alarms (🚨).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.cli as cli_mod
from src.config import get_settings
from src.db.models import Platform
from src.scrapers import orchestrator, removal_guard


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

        async def fake_links(*, limit, deadline=None, hides_allowed=True, guard_dir=None):
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


async def test_link_check_outage_is_alarm_not_warning(db, heal_rolling_links):
    """Most probes failing is an outage, not noise: dead listings may stay up unverified."""
    idle_heal = orchestrator.HealResult(selected=0, fixed=0, remaining=0, errors=0)
    idle_rolling = orchestrator.RollingRefreshResult(
        selected=0, refreshed=0, errors=0, price_changes=[],
    )

    lines = await _nightly_lines(
        heal_rolling_links, idle_heal, idle_rolling,
        orchestrator.LinkVerifyStats(checked=10, errors=10, inconclusive=10),
    )
    link_line = next(line for line in lines if "ссылки: проверено" in line)
    assert link_line.startswith("🚨 ")
    assert any("🚨" in line for line in lines)  # the night's maintenance_failed trigger

    lines = await _nightly_lines(
        heal_rolling_links, idle_heal, idle_rolling,
        orchestrator.LinkVerifyStats(checked=5, errors=1, inconclusive=1),
    )
    link_line = next(line for line in lines if "ссылки: проверено" in line)
    assert link_line.startswith("⚠️ ")
    assert not any("🚨" in line for line in lines)


async def test_liveness_probe_outage_is_alarm(monkeypatch):
    from src.site import liveness

    monkeypatch.setattr(get_settings(), "site_liveness_enabled", True)

    async def no_notice(report):
        return False

    def sweep(*, errors, checked):
        async def fake_sweep(*, max_hides=None, auto_hide=None):
            return SimpleNamespace(
                hidden=[], candidates_confirmed=[], candidates_unconfirmed=[],
                kept_active=0, kept_pinned=0, checked=checked, errors=errors, skipped=0,
            )

        return fake_sweep

    monkeypatch.setattr(liveness, "send_liveness_notice", no_notice)
    monkeypatch.setattr(liveness, "site_liveness_sweep", sweep(errors=10, checked=10))
    assert (await cli_mod._liveness_nightly_line(notify=False)).startswith("🚨 liveness")

    monkeypatch.setattr(liveness, "site_liveness_sweep", sweep(errors=1, checked=100))
    line = await cli_mod._liveness_nightly_line(notify=False)
    assert line.startswith("⚠️ liveness") and "ошибок 1" in line


async def test_liveness_source_failures_are_not_probe_failures(monkeypatch):
    from src.site import liveness

    monkeypatch.setattr(get_settings(), "site_liveness_enabled", True)

    async def no_notice(report):
        return False

    def sweep(**counts):
        async def fake_sweep(*, max_hides=None, auto_hide=None):
            return SimpleNamespace(
                hidden=[], candidates_confirmed=[], candidates_unconfirmed=[],
                kept_active=0, kept_pinned=0, skipped=0, **counts,
            )

        return fake_sweep

    monkeypatch.setattr(liveness, "send_liveness_notice", no_notice)
    # Every source failed before its first probe: the sweep never ran.
    monkeypatch.setattr(
        liveness, "site_liveness_sweep", sweep(checked=0, errors=3, source_errors=3),
    )
    assert (await cli_mod._liveness_nightly_line(notify=False)).startswith("🚨 liveness")
    # Probes ran and two sources timed out: a source timeout is not a network outage.
    monkeypatch.setattr(
        liveness, "site_liveness_sweep", sweep(checked=10, errors=2, source_errors=2),
    )
    assert (await cli_mod._liveness_nightly_line(notify=False)).startswith("⚠️ liveness")
    # Ten probes, four failed, two sources timed out: source failures stay out of the share.
    monkeypatch.setattr(
        liveness, "site_liveness_sweep", sweep(checked=10, errors=6, source_errors=2),
    )
    assert (await cli_mod._liveness_nightly_line(notify=False)).startswith("⚠️ liveness")


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


def test_real_item_errors_degrade_the_source_and_fail_the_night():
    """Pins the item-error policy with real ScrapeResult objects, not SimpleNamespace fakes.

    Item errors at least half of the processed items make the source "degraded" (run_safety
    evaluate_source, reason item_error_rate), and _nightly_unhealthy fails the night on "degraded".
    """
    healthy = dict(
        dead=[], low_coverage=[], degraded=[], parse_degraded=[], sweep_reasons=[],
        maintenance_failed=False,
    )
    with_errors = orchestrator.ScrapeResult(
        platform=Platform.NEKRETNINE, previewed=10, fetched=3, errors=7,
    )
    assert with_errors.outcome == "degraded"
    assert cli_mod._nightly_unhealthy(**healthy, results=[with_errors])

    clean = orchestrator.ScrapeResult(platform=Platform.NEKRETNINE, previewed=10, fetched=10)
    assert clean.outcome == "ok"
    assert not cli_mod._nightly_unhealthy(**healthy, results=[clean])
    assert cli_mod._nightly_unhealthy(**{**healthy, "maintenance_failed": True}, results=[clean])

    failed = orchestrator.ScrapeResult(platform=Platform.NEKRETNINE, previewed=10, errors=1)
    assert failed.outcome == "failed"
    assert cli_mod._nightly_unhealthy(**healthy, results=[failed])


def _public_refresh(monkeypatch, **fields):
    values = {"selected": 10, "refreshed": 8, "price_changes": [], "deactivated": 0, **fields}
    recorded = []

    async def fake_rolling(*, limit, public_only, scrapers, deadline):
        return SimpleNamespace(**values)

    def capture(_state_dir, step, **kwargs):
        recorded.append({"step": step, **kwargs})

    monkeypatch.setattr(orchestrator, "rolling_detail_refresh", fake_rolling)
    # The guard writes go to the state dir: keep them in memory and hand the calls back.
    monkeypatch.setattr(removal_guard, "write_step", capture)
    return recorded


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


async def test_public_refresh_mass_removal_alarms_but_exits_zero(monkeypatch, capsys):
    """A changed URL scheme deactivates most attempted listings: print the count and alarm. The
    public run exits 0, because exit 1 for a mass removal applies at night only."""
    _public_refresh(monkeypatch, errors=0, skipped=0, source_errors={}, deactivated=9)
    await cli_mod._refetch_details(None, False, None, False, public_only=True)
    out = capsys.readouterr().out
    assert "деактивировано (404/410): 9" in out
    assert "🚨 Массовое снятие: 9 из 17" in out


async def test_public_refresh_few_removals_are_counted_without_alarm(monkeypatch, capsys):
    _public_refresh(monkeypatch, errors=0, skipped=0, source_errors={}, deactivated=1)
    await cli_mod._refetch_details(None, False, None, False, public_only=True)
    out = capsys.readouterr().out
    assert "деактивировано (404/410): 1" in out
    assert "🚨" not in out


async def test_public_refresh_removal_share_counts_live_attempts_only(monkeypatch, capsys):
    """Errors are live attempts: 4 removals of 14 is not above 0.3. Re-checks of listings already
    removed are not attempts at all: 4 removals of 10 live listings alarm, however many repeats the
    run carried."""
    _public_refresh(
        monkeypatch, refreshed=6, errors=4, skipped=0, source_errors={}, deactivated=4,
    )
    await cli_mod._refetch_details(None, False, None, False, public_only=True)
    assert "🚨" not in capsys.readouterr().out
    _public_refresh(
        monkeypatch, refreshed=6, errors=0, skipped=0, source_errors={}, deactivated=4,
        already_removed=20,
    )
    await cli_mod._refetch_details(None, False, None, False, public_only=True)
    out = capsys.readouterr().out
    assert "🚨 Массовое снятие: 4 из 10 попыток" in out
    assert "повторно подтверждено снятых: 20" in out


async def test_nightly_mass_removal_is_alarm_with_count(db, heal_rolling_links):
    idle_heal = orchestrator.HealResult(selected=0, fixed=0, remaining=0, errors=0)

    lines = await _nightly_lines(
        heal_rolling_links, idle_heal,
        orchestrator.RollingRefreshResult(
            selected=10, refreshed=1, errors=0, price_changes=[], deactivated=9,
        ),
        orchestrator.LinkVerifyStats(),
    )
    detail_line = next(line for line in lines if "деталей обновлено" in line)
    assert detail_line.startswith("🚨 ")
    assert "деактивировано (404/410): 9" in detail_line
    assert any("🚨" in line for line in lines)  # the night's maintenance_failed trigger

    lines = await _nightly_lines(
        heal_rolling_links, idle_heal,
        orchestrator.RollingRefreshResult(
            selected=10, refreshed=9, errors=0, price_changes=[], deactivated=1,
        ),
        orchestrator.LinkVerifyStats(),
    )
    detail_line = next(line for line in lines if "деталей обновлено" in line)
    assert not detail_line.startswith("🚨 ")
    assert "деактивировано (404/410): 1" in detail_line
    assert not any("🚨" in line for line in lines)



async def test_public_refresh_mass_removal_is_recorded_as_an_alarm(monkeypatch, capsys):
    recorded = _public_refresh(monkeypatch, errors=0, skipped=0, source_errors={}, deactivated=9)
    await cli_mod._refetch_details(None, False, None, False, public_only=True)
    outcome = [c for c in recorded if c["step"] == "public" and c["status"] != "running"]
    assert len(outcome) == 1 and outcome[0]["status"] == "alarm"
    assert outcome[0]["deactivated"] == 9 and outcome[0]["attempted"] == 17


async def test_public_refresh_clean_run_is_recorded_as_clean(monkeypatch, capsys):
    recorded = _public_refresh(monkeypatch, errors=0, skipped=0, source_errors={}, deactivated=1)
    await cli_mod._refetch_details(None, False, None, False, public_only=True)
    outcome = [c for c in recorded if c["step"] == "public" and c["status"] != "running"]
    assert [c["status"] for c in outcome] == ["clean"]


async def test_public_refresh_crash_is_recorded_as_failed(monkeypatch, capsys):
    recorded = _public_refresh(monkeypatch, errors=0, skipped=0, source_errors={})

    async def crashing_rolling(**_kwargs):
        raise RuntimeError("detail refresh exploded")

    monkeypatch.setattr(orchestrator, "rolling_detail_refresh", crashing_rolling)
    with pytest.raises(RuntimeError):
        await cli_mod._refetch_details(None, False, None, False, public_only=True)
    assert [c["status"] for c in recorded if c["step"] == "public"] == ["running", "failed"]


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
