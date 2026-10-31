"""Bot result texts: kept / removed / not found / failed are told apart (WM05-W3, B1).

Report shapes in tests/fixtures/wm_n08_reports.json are real CleanReport.to_dict() values saved
by R04 N08. No bot, no network, no model.
"""
# ruff: noqa: RUF001  (Russian user-facing texts are asserted verbatim)

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from src.telegram import wm_bot, wm_summary
from src.watermark.pipeline import CleanReport, MarkResult
from src.watermark.store import StoredResult

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "wm_n08_reports.json").read_text(encoding="utf-8")
)["cases"]
KEPT_REASONS = ["corner_mode", "disabled", "self_check", "lama_unavailable"]
KEPT_SENTENCE = "Знак найден и оставлен без изменений"
HAS_KEPT_REASON = "kept_reason" in {f.name for f in dataclasses.fields(MarkResult)}


def _report(name: str) -> CleanReport:
    raw = FIXTURES[name]
    return CleanReport(
        width=raw["width"],
        height=raw["height"],
        marks=[MarkResult(**m) for m in raw["marks"]],
        seconds=raw["seconds"],
    )


def _mark(method: str, *, platform: str = "halooglasi", **extra: Any) -> MarkResult:
    return MarkResult(
        template_id="t", platform=platform, instance=0, score=0.9, x=1.0, y=2.0, scale=1.0,
        method=method, residual=0.0, **extra,
    )


def _outcome(name: str) -> wm_summary.PhotoOutcome:
    return wm_summary.outcome_from_report(_report(name))


# --- one photo: the five mutually exclusive statuses --------------------------------


def test_only_kept_photo_is_kept_only_not_unmarked():
    outcome = _outcome("only_kept_halo_000")
    assert outcome.status == "kept_only"
    assert dict(outcome.kept) == {"Halo Oglasi": 1} and dict(outcome.removed) == {}


def test_mixed_zida_043_is_three_found_two_removed_one_kept():
    outcome = _outcome("mixed_zida_043")
    assert outcome.status == "mixed"
    assert dict(outcome.removed) == {"4zida": 2}
    assert dict(outcome.kept) == {"Halo Oglasi": 1}


def test_fully_removed_and_not_found_and_failed():
    assert _outcome("removed_zida_001").status == "removed_only"
    assert _outcome("no_marks").status == "not_found"
    assert wm_summary.outcome_from_report(None).status == "failed"


# --- texts ------------------------------------------------------------------------


def test_only_kept_says_found_and_kept_never_no_marks():
    summary = wm_summary.summarize_outcomes([_outcome("only_kept_halo_000")])
    assert summary.text.startswith(KEPT_SENTENCE)
    assert "Удалено: 0" in summary.text and "оставлено: Halo Oglasi — 1" in summary.text
    assert wm_bot.NOTHING_FOUND not in summary.text and "без найденных знаков" not in summary.text
    assert summary.send_files is False  # no pixel changed: nothing new to send


def test_mixed_photo_lists_removed_and_kept_separately():
    summary = wm_summary.summarize_outcomes([_outcome("mixed_zida_043")])
    assert "Удалено: 4zida — 2; оставлено: Halo Oglasi — 1" in summary.text
    assert summary.marks_found == 3 and summary.marks_removed == 2 and summary.marks_kept == 1
    assert summary.send_files is True


def test_removed_only_keeps_the_platform_counts_format():
    summary = wm_summary.summarize_outcomes([_outcome("removed_zida_001")])
    assert "Удалено: 4zida — 2" in summary.text
    assert "оставлено" not in summary.text and "Не удалось" not in summary.text


def test_nothing_found_uses_the_existing_text():
    summary = wm_summary.summarize_outcomes([_outcome("no_marks"), _outcome("no_marks")])
    assert summary.text == wm_bot.NOTHING_FOUND and summary.send_files is False


def test_album_of_ten_partition_sums_to_album_size_and_fits_a_caption():
    outcomes = (
        [_outcome("mixed_zida_043"), _outcome("only_kept_halo_000")]
        + [_outcome("no_marks")] * 7
        + [wm_summary.failed_outcome()]
    )
    summary = wm_summary.summarize_outcomes(outcomes)
    assert summary.total == 10
    assert summary.photos == {
        "removed_only": 0, "kept_only": 1, "mixed": 1, "not_found": 7, "failed": 1,
    }
    assert sum(summary.photos.values()) == summary.total
    assert (summary.marks_found, summary.marks_removed, summary.marks_kept) == (4, 2, 2)
    # kept photos are not "without marks": 7 of 10, not 8 or 9
    assert "без найденных знаков: 7 из 10" in summary.text
    assert "Не удалось обработать: 1 из 10" in summary.text
    assert len(summary.text) < 1024


def test_failed_photo_is_never_reported_as_without_marks():
    summary = wm_summary.summarize_outcomes(
        [_outcome("removed_zida_001"), wm_summary.failed_outcome(original_kept=True)]
    )
    assert summary.photos["failed"] == 1 and summary.photos["not_found"] == 0
    assert "Не удалось обработать: 1 из 2" in summary.text and "оригинал" in summary.text
    assert "без найденных знаков" not in summary.text
    assert summary.send_files is True


def test_only_failed_photos_still_return_the_originals():
    summary = wm_summary.summarize_outcomes([wm_summary.failed_outcome(original_kept=True)])
    assert summary.text.startswith("Не удалось обработать: 1 из 1")
    assert "Знаков площадок не нашёл" not in summary.text
    assert summary.send_files is True


def test_failed_without_original_does_not_promise_one():
    summary = wm_summary.summarize_outcomes([wm_summary.failed_outcome(original_kept=False)])
    assert "оригинал" not in summary.text and summary.send_files is False


def test_failed_next_to_not_found_is_not_nothing_found():
    summary = wm_summary.summarize_outcomes(
        [_outcome("no_marks"), wm_summary.failed_outcome(original_kept=True)]
    )
    assert summary.text != wm_bot.NOTHING_FOUND
    assert "без найденных знаков: 1 из 2" in summary.text.lower()


# --- kept_reason (contract of W1/W2): every value reads as "kept" -------------------


@pytest.mark.skipif(not HAS_KEPT_REASON, reason="MarkResult.kept_reason arrives with W1a")
@pytest.mark.parametrize("reason", [*KEPT_REASONS, None])
def test_every_kept_reason_gets_the_same_kept_text(reason):
    report = CleanReport(800, 600, marks=[_mark("skip", kept_reason=reason)])
    summary = wm_summary.summarize_outcomes([wm_summary.outcome_from_report(report)])
    assert summary.text.startswith(KEPT_SENTENCE)
    assert summary.photos["kept_only"] == 1 and summary.marks_kept == 1
    assert not any(word in summary.text for word in (*KEPT_REASONS, "None"))


def test_unknown_future_kept_reason_does_not_break_the_text():
    mark = _mark("skip")
    object.__setattr__(mark, "kept_reason", "something_new")
    summary = wm_summary.summarize_outcomes(
        [wm_summary.outcome_from_report(CleanReport(1, 1, marks=[mark]))]
    )
    assert summary.text.startswith(KEPT_SENTENCE)


# --- restoring a stored report ------------------------------------------------------


def test_stored_report_without_clean_copy_keeps_its_kept_marks():
    result = StoredResult(
        original=Path("/x/0.jpg"), clean=None, report=FIXTURES["only_kept_halo_000"], reused=True
    )
    report = wm_bot._report_from_dict(result)
    assert [m.method for m in report.marks] == ["skip"]
    assert wm_summary.outcome_from_report(report).status == "kept_only"


def test_stored_mixed_report_is_not_described_as_all_removed():
    result = StoredResult(
        original=Path("/x/0.jpg"),
        clean=Path("/y/0.jpg"),
        report=FIXTURES["mixed_zida_043"],
        reused=True,
    )
    summary = wm_summary.summarize_outcomes(
        [wm_summary.outcome_from_report(wm_bot._report_from_dict(result))]
    )
    assert summary.marks_found == 3 and summary.marks_kept == 1


def test_old_sidecar_without_kept_reason_and_unknown_keys_still_loads():
    raw = json.loads(json.dumps(FIXTURES["only_kept_halo_000"]))
    raw["marks"][0]["something_from_a_newer_version"] = 1
    result = StoredResult(original=Path("/x/0.jpg"), clean=None, report=raw, reused=True)
    assert len(wm_bot._report_from_dict(result).marks) == 1
