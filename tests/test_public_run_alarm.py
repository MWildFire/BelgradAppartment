"""The public detail run's mass-removal alarm reaches the night's hide decisions (Q3.21)."""

from datetime import UTC, datetime, timedelta

from src.scrapers.run_safety import (
    PUBLIC_RUN_ALARM_HOURS,
    public_run_alarm_recent,
    record_public_run_alarm,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def test_missing_record_means_no_alarm(tmp_path):
    assert public_run_alarm_recent(tmp_path / "state" / "public_run_alarm.json", now=NOW) is False


def test_recorded_alarm_withholds_hides_inside_the_window(tmp_path):
    path = tmp_path / "public_run_alarm.json"
    record_public_run_alarm(path, deactivated=6, attempted=10, now=NOW)
    later = NOW + timedelta(hours=PUBLIC_RUN_ALARM_HOURS - 1)
    assert public_run_alarm_recent(path, now=later) is True


def test_alarm_expires_after_the_window(tmp_path):
    path = tmp_path / "public_run_alarm.json"
    record_public_run_alarm(path, deactivated=6, attempted=10, now=NOW)
    assert public_run_alarm_recent(path, now=NOW + timedelta(hours=PUBLIC_RUN_ALARM_HOURS)) is False


def test_unreadable_record_fails_closed(tmp_path):
    path = tmp_path / "public_run_alarm.json"
    path.write_text("{not json", encoding="utf-8")
    assert public_run_alarm_recent(path, now=NOW) is True


def test_record_is_replaced_atomically_and_the_latest_alarm_counts(tmp_path):
    path = tmp_path / "public_run_alarm.json"
    record_public_run_alarm(path, deactivated=6, attempted=10, now=NOW)
    record_public_run_alarm(path, deactivated=7, attempted=11, now=NOW + timedelta(hours=1))
    assert not (tmp_path / "public_run_alarm.tmp").exists()
    assert public_run_alarm_recent(path, now=NOW + timedelta(hours=2)) is True
