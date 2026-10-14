"""Nightly summary alarm levels.

Item errors below half of the items a source processed, budget skips, and the expected rolling
source limits (TimeoutError, NoProxyAvailableError, source_budget_exhausted) are warnings (⚠️).
At least half of the items failing makes the source degraded, and that fails the night (PR #6,
owner decision Q3.16). Skipped pages and anti-bot coverage gaps do not (Q3.9). Unexpected
source-level failures, mass removals, skipped link checks and failed steps stay alarms (🚨).
"""

from __future__ import annotations

from datetime import UTC, datetime
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
            source_errors={"NEKRETNINE": "RefreshError"},
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
    evaluate_source, reason item_error_rate), and _nightly_unhealthy fails the night on that reason.
    """
    healthy = dict(
        dead=[], low_coverage=[], parse_degraded=[], sweep_reasons=[],
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


async def test_public_refresh_source_failure_records_a_failed_step(monkeypatch):
    """A run that exits 1 for a source failure must not leave the guard reading it as clean."""
    recorded = _public_refresh(
        monkeypatch, errors=0, skipped=0, source_errors={"NEKRETNINE": "TimeoutError"},
    )
    with pytest.raises(SystemExit):
        await cli_mod._refetch_details(None, False, None, False, public_only=True)
    assert recorded[-1]["status"] == removal_guard.FAILED


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


# --- Q3.9: an expected anti-bot gap is a warning; real failures still fail the night ---


def _ids(count):
    return {str(number) for number in range(count)}


def _source(**fields):
    """A full-crawl ScrapeResult with a complete, trusted walk, unless a field says otherwise."""
    previewed = fields.get("previewed", 100)
    base = {
        "platform": Platform.NEKRETNINE, "full_crawl": True, "crawl_complete": True,
        "stop_reason": "complete", "pages_attempted": 10, "denominator_trusted": True,
        "site_total": previewed, "previewed": previewed, "fetched": previewed,
        "observed_ids": _ids(previewed),
    }
    return orchestrator.ScrapeResult(**{**base, **fields})


def _night_13_10():
    """The sources of the 13.10 night as the prod run record shows them (counts only)."""
    return [
        _source(platform=Platform.NEKRETNINE, previewed=3339, fetched=3335, errors=4,
                site_total=3342, crawl_complete=False),
        _source(platform=Platform.CETIRI_ZIDA, previewed=3312, site_total=3373,
                crawl_complete=False, denominator_trusted=False, skipped_pages=1, pages_failed=1),
        _source(platform=Platform.HALOOGLASI, previewed=2531, site_total=2604,
                crawl_complete=False),
        _source(platform=Platform.CITYEXPERT, previewed=581),
    ]


def _unhealthy(results, *, maintenance_failed=False, dead=(), low_coverage=(), parse_degraded=(),
               sweep_reasons=()):
    return cli_mod._nightly_unhealthy(
        dead=list(dead), low_coverage=list(low_coverage),
        parse_degraded=list(parse_degraded), sweep_reasons=list(sweep_reasons),
        maintenance_failed=maintenance_failed, results=results,
    )


@pytest.mark.parametrize(
    "source_error", ["TimeoutError", "NoProxyAvailableError", "source_budget_exhausted"],
)
async def test_night_with_expected_anti_bot_gaps_does_not_fail(db, heal_rolling_links, source_error):
    lines = await _nightly_lines(
        heal_rolling_links,
        orchestrator.HealResult(selected=0, fixed=0, remaining=0, errors=0),
        orchestrator.RollingRefreshResult(
            selected=10, refreshed=9, errors=0, price_changes=[],
            source_errors={"NEKRETNINE": source_error},
        ),
        orchestrator.LinkVerifyStats(),
    )
    detail_line = next(line for line in lines if "деталей обновлено" in line)
    assert detail_line.startswith("⚠️ ") and f"NEKRETNINE: {source_error}" in detail_line
    results = _night_13_10()
    assert not _unhealthy(results, maintenance_failed=any("🚨" in line for line in lines))
    assert not any(result.sweep_allowed for result in results[:3])


def test_skipped_pages_alone_do_not_fail_the_night():
    """Skipped pages are a partial walk (Q3.9): by themselves they do not fail the night.

    The ⚠️ «Частичный обход» line is written by _nightly_refresh_impl and is not checked here.
    """
    assert not _unhealthy([_source(skipped_pages=1, pages_failed=1)])


@pytest.mark.parametrize("source", [
    pytest.param({"previewed": 10, "fetched": 3, "errors": 7}, id="item_error_rate"),
    pytest.param({"previewed": 100, "fetched": 90}, id="items_unprocessed"),
    pytest.param({"denominator_trusted": False, "site_total": None}, id="no_denominator"),
    pytest.param({"crawl_complete": False, "previewed": 5, "site_total": 0}, id="zero_denominator"),
    pytest.param({"quality_reasons": ["parse_degraded"]}, id="other_quality_reason"),
    pytest.param({"previewed": 10, "fetched": 0, "errors": 1}, id="failed"),
    pytest.param({"denominator_trusted": False, "site_total": -1}, id="negative_denominator"),
    pytest.param({"stop_reason": "item_limit"}, id="item_limit"),
    pytest.param({"observed_ids": _ids(50)}, id="missing_presence_evidence"),
    pytest.param({"pages_attempted": 0}, id="missing_page_evidence"),
])
def test_real_source_failures_fail_the_night(source):
    assert _unhealthy([_source(**source)])


def test_dead_low_coverage_parser_and_sweep_failures_fail_the_night():
    clean = [_source()]
    assert not _unhealthy(clean)
    assert _unhealthy(clean, dead=["CITYEXPERT"])
    assert _unhealthy(clean, low_coverage=["HALOOGLASI (1000/2604)"])
    assert _unhealthy(clean, parse_degraded=["parse-rate 40% (2/5)"])
    assert _unhealthy(clean, sweep_reasons=["probe_sweep_broken"])


@pytest.mark.parametrize("source_errors", [
    {"NEKRETNINE": "RefreshError"},
    {"NEKRETNINE": "TargetClosedError"},
    {"NEKRETNINE": "TimeoutError", "CETIRI_ZIDA": "RefreshError"},
])
async def test_unexpected_source_error_is_alarm_and_fails_the_night(
    db, heal_rolling_links, source_errors,
):
    lines = await _nightly_lines(
        heal_rolling_links,
        orchestrator.HealResult(selected=0, fixed=0, remaining=0, errors=0),
        orchestrator.RollingRefreshResult(
            selected=10, refreshed=9, errors=0, price_changes=[], source_errors=source_errors,
        ),
        orchestrator.LinkVerifyStats(),
    )
    detail_line = next(line for line in lines if "деталей обновлено" in line)
    assert detail_line.startswith("🚨 ")
    assert all(f"{platform}: {code}" in detail_line for platform, code in source_errors.items())
    assert _unhealthy(_night_13_10(), maintenance_failed=any("🚨" in line for line in lines))


async def test_nightly_mass_removal_fails_the_night(db, heal_rolling_links):
    lines = await _nightly_lines(
        heal_rolling_links,
        orchestrator.HealResult(selected=0, fixed=0, remaining=0, errors=0),
        orchestrator.RollingRefreshResult(
            selected=10, refreshed=1, errors=0, price_changes=[], deactivated=9,
        ),
        orchestrator.LinkVerifyStats(),
    )
    assert _unhealthy(_night_13_10(), maintenance_failed=any("🚨" in line for line in lines))


async def test_public_run_failure_fails_the_night(db, heal_rolling_links, monkeypatch):
    monkeypatch.setattr(
        removal_guard, "fresh_alarm",
        lambda _state_dir, step, **_kwargs: (
            (removal_guard.FAILED, datetime.now(UTC)) if step == "public" else None
        ),
    )
    lines = await _nightly_lines(
        heal_rolling_links,
        orchestrator.HealResult(selected=0, fixed=0, remaining=0, errors=0),
        orchestrator.RollingRefreshResult(selected=0, refreshed=0, errors=0, price_changes=[]),
        orchestrator.LinkVerifyStats(),
    )
    assert any(line.startswith("🚨 публичный прогон") for line in lines)
    assert _unhealthy([_source()], maintenance_failed=any("🚨" in line for line in lines))


def test_expected_gap_keeps_the_outcome_and_the_sweep_blocked():
    """Q3.9 changes only the exit: evaluate_source still degrades those sources and blocks sweeps."""
    results = _night_13_10()
    assert [(r.outcome, r.sweep_allowed) for r in results] == [
        ("degraded", False), ("degraded", False), ("degraded", False), ("ok", True),
    ]



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
