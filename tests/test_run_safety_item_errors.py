"""Item errors are warnings for the night, while budget skips and failures still degrade."""

from __future__ import annotations

from types import SimpleNamespace

from src.cli import _nightly_unhealthy
from src.scrapers.run_safety import evaluate_source


def _source(**fields):
    base = {
        "errors": 0,
        "skipped_pages": 0,
        "pages_failed": 0,
        "stop_reason": "complete",
        "full_crawl": False,
        "crawl_complete": False,
        "denominator_trusted": False,
        "site_total": None,
        "previewed": 100,
        "observed_ids": set(range(100)),
        "fetched": 100,
        "pages_attempted": 10,
        "quality_reasons": [],
    }
    return SimpleNamespace(**{**base, **fields})


def test_item_errors_alone_keep_the_source_ok_but_block_the_sweep():
    evaluation = evaluate_source(_source(errors=7, fetched=93))

    assert evaluation.outcome == "ok"
    assert evaluation.reasons == ("item_errors",)
    assert not evaluation.sweep_allowed


def test_item_errors_do_not_fail_the_night_with_the_real_outcome():
    healthy = {
        "dead": [],
        "low_coverage": [],
        "degraded": [],
        "parse_degraded": [],
        "sweep_reasons": [],
        "maintenance_failed": False,
    }
    outcome = evaluate_source(_source(errors=7, fetched=93)).outcome

    assert not _nightly_unhealthy(**healthy, results=[SimpleNamespace(outcome=outcome, errors=7)])


def test_budget_skips_still_degrade_the_source():
    evaluation = evaluate_source(_source(skipped_pages=2))

    assert evaluation.outcome == "degraded"
    assert "skipped_pages" in evaluation.reasons


def test_item_errors_at_half_of_the_items_degrade_the_source():
    evaluation = evaluate_source(_source(errors=50, fetched=50))

    assert evaluation.outcome == "degraded"
    assert "item_error_rate" in evaluation.reasons


def test_item_errors_below_half_of_the_items_keep_the_source_ok():
    evaluation = evaluate_source(_source(errors=49, fetched=51))

    assert evaluation.outcome == "ok"
    assert "item_error_rate" not in evaluation.reasons


def test_near_total_item_failure_fails_the_night():
    healthy = {
        "dead": [],
        "low_coverage": [],
        "degraded": [],
        "parse_degraded": [],
        "sweep_reasons": [],
        "maintenance_failed": False,
    }
    outcome = evaluate_source(_source(errors=99, fetched=1)).outcome

    assert _nightly_unhealthy(**healthy, results=[SimpleNamespace(outcome=outcome, errors=99)])
