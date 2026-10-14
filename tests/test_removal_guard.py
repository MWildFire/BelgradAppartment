"""The removal guard (Q3.21): every removal step records its outcome, and auto-hides need them clean."""

from datetime import UTC, datetime, timedelta

import pytest

from src.scrapers import removal_guard as guard

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
STEPS = ("public", "rolling", "verify")


def _write(state_dir, step, status, at=NOW):
    guard.write_step(state_dir, step, status=status, now=at)


def test_missing_outcomes_withhold_until_every_step_has_finished_once(tmp_path):
    assert guard.withholds_hides(tmp_path, now=NOW, steps=STEPS) is True
    for step in STEPS:
        _write(tmp_path, step, guard.CLEAN)
    assert guard.withholds_hides(tmp_path, now=NOW, steps=STEPS) is False


def test_an_alarm_holds_against_later_clean_and_running_writes_inside_the_window(tmp_path):
    _write(tmp_path, "public", guard.ALARM)
    _write(tmp_path, "rolling", guard.CLEAN)
    _write(tmp_path, "verify", guard.CLEAN)
    later = NOW + timedelta(hours=3)
    _write(tmp_path, "public", guard.CLEAN, at=later)
    _write(tmp_path, "public", guard.RUNNING, at=later)
    assert guard.withholds_hides(tmp_path, now=later, steps=STEPS) is True


def test_a_new_clean_outcome_releases_once_the_window_has_closed(tmp_path):
    _write(tmp_path, "public", guard.ALARM)
    _write(tmp_path, "rolling", guard.CLEAN)
    _write(tmp_path, "verify", guard.CLEAN)
    after = NOW + timedelta(hours=guard.GUARD_HOURS + 1)
    for step in STEPS:
        _write(tmp_path, step, guard.CLEAN, at=after)
    assert guard.withholds_hides(tmp_path, now=after, steps=STEPS) is False


def test_a_failed_step_holds_the_guard(tmp_path):
    for step in STEPS:
        _write(tmp_path, step, guard.CLEAN)
    guard.record_failure(tmp_path, "rolling", now=NOW + timedelta(hours=1))
    assert guard.withholds_hides(tmp_path, now=NOW + timedelta(hours=1), steps=STEPS) is True


def test_a_running_record_left_by_a_crash_withholds(tmp_path):
    _write(tmp_path, "public", guard.RUNNING)
    _write(tmp_path, "rolling", guard.CLEAN)
    _write(tmp_path, "verify", guard.CLEAN)
    assert guard.withholds_hides(tmp_path, now=NOW, steps=STEPS) is True


def test_a_stale_clean_outcome_withholds(tmp_path):
    for step in STEPS:
        _write(tmp_path, step, guard.CLEAN)
    later = NOW + timedelta(hours=guard.GUARD_HOURS)
    assert guard.withholds_hides(tmp_path, now=later, steps=STEPS) is True


def test_an_unreadable_or_future_outcome_fails_closed(tmp_path):
    for step in STEPS:
        _write(tmp_path, step, guard.CLEAN)
    (tmp_path / "removal_guard" / "verify.json").write_text("{not json", encoding="utf-8")
    assert guard.withholds_hides(tmp_path, now=NOW, steps=STEPS) is True
    _write(tmp_path, "verify", guard.CLEAN, at=NOW + timedelta(hours=1))
    assert guard.withholds_hides(tmp_path, now=NOW, steps=STEPS) is True


def test_a_failed_failure_record_is_logged_not_raised(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(guard, "write_step", broken)
    guard.record_failure(tmp_path, "public", now=NOW)


def test_a_future_alarm_still_holds_against_a_clean_write(tmp_path):
    _write(tmp_path, "public", guard.ALARM, at=NOW + timedelta(hours=1))
    guard.write_step(tmp_path, "public", status=guard.CLEAN, now=NOW)
    assert guard.withholds_hides(tmp_path, now=NOW, steps=("public",)) is True


def test_an_unreadable_record_holds_for_the_window_then_releases(tmp_path):
    import os

    path = tmp_path / "removal_guard" / "public.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    recent = (NOW - timedelta(hours=1)).timestamp()
    os.utime(path, (recent, recent))
    guard.write_step(tmp_path, "public", status=guard.CLEAN, now=NOW)
    assert path.read_text(encoding="utf-8") == "{not json"
    later = NOW + timedelta(hours=guard.GUARD_HOURS)
    guard.write_step(tmp_path, "public", status=guard.CLEAN, now=later)
    assert guard.withholds_hides(tmp_path, now=later, steps=("public",)) is False


@pytest.mark.parametrize("record", [
    {"at": NOW.isoformat()},
    {"at": NOW.isoformat(), "status": None},
    {"at": NOW.isoformat(), "status": "weird"},
])
def test_a_record_without_a_known_status_holds_instead_of_raising(tmp_path, record):
    import json
    import os

    for step in STEPS:
        _write(tmp_path, step, guard.CLEAN)
    path = tmp_path / "removal_guard" / "verify.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    recent = (NOW - timedelta(hours=1)).timestamp()
    os.utime(path, (recent, recent))
    assert guard.withholds_hides(tmp_path, now=NOW, steps=STEPS) is True
    guard.write_step(tmp_path, "verify", status=guard.CLEAN, now=NOW)
    assert json.loads(path.read_text(encoding="utf-8")) == record


@pytest.mark.parametrize(
    ("status", "age", "expected"),
    [
        (guard.ALARM, timedelta(hours=1), guard.ALARM),
        (guard.FAILED, timedelta(hours=23, minutes=59), guard.FAILED),
        (guard.ALARM, timedelta(hours=24), None),
        (guard.ALARM, timedelta(hours=-1), None),
        (guard.CLEAN, timedelta(hours=1), None),
    ],
    ids=["alarm-1h", "failed-23h59", "alarm-24h", "alarm-future", "clean-1h"],
)
def test_fresh_alarm_reports_an_alarm_or_failure_younger_than_a_day(
    tmp_path, status, age, expected
):
    at = NOW - age
    _write(tmp_path, "public", status, at=at)
    found = guard.fresh_alarm(tmp_path, "public", now=NOW)
    assert found == (None if expected is None else (expected, at))


def test_fresh_alarm_is_none_without_a_record(tmp_path):
    assert guard.fresh_alarm(tmp_path, "public", now=NOW) is None


def test_fresh_alarm_ignores_an_unreadable_record(tmp_path):
    _write(tmp_path, "public", guard.ALARM)
    (tmp_path / "removal_guard" / "public.json").write_text("{not json", encoding="utf-8")
    assert guard.fresh_alarm(tmp_path, "public", now=NOW) is None
