from contextlib import suppress
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.admin.operational_health import build_operations
from src.config import get_settings
from src.db.models import Listing, Platform
from src.db.session import get_session


async def test_one_fresh_attempt_does_not_hide_unverified_ingestion(db, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    now = datetime.now(UTC)
    async with get_session() as session:
        for i in range(3):
            session.add(
                Listing(
                    platform=Platform.NEKRETNINE,
                    external_id=str(i),
                    url=f"https://x/{i}",
                    last_seen_at=now,
                    details_refreshed_at=now,
                    details_succeeded_at=now if i == 0 else None,
                )
            )
    async with get_session() as session:
        report = await build_operations(session, now=now)
    source = next(row for row in report["sources"] if row["platform"] == "NEKRETNINE")
    assert source["fresh_7d"] == 1
    assert source["full_overdue"] is True
    assert report["status"] == "degraded"
    assert report["backup_verified"] is False


async def test_readiness_fails_if_database_unavailable(monkeypatch):
    from src.site.routes import readiness

    class FailedSession:
        async def __aenter__(self):
            raise OSError("database unavailable")

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr("src.site.db.read_session", FailedSession)
    assert (await readiness()).status_code == 503


async def test_cancelled_refresh_has_durable_failed_outcome(monkeypatch, tmp_path):
    import asyncio

    import src.cli as cli
    from src.jobs.run_history import read_runs

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(cli, "_nightly_refresh_impl", AsyncMock(side_effect=asyncio.CancelledError))
    with suppress(asyncio.CancelledError):
        await cli._nightly_refresh(1, 3, True, False, no_notify=True)
    runs = read_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["error_code"] == "CancelledError"
    assert runs[0]["finished_at"]


@pytest.mark.parametrize("offset_hours,expected", [(-1, True), (1, False), (-37, False)])
async def test_backup_verification_rejects_future_and_old_receipts(
    db,
    monkeypatch,
    tmp_path,
    offset_hours,
    expected,
):
    import json

    from src.admin import operational_health

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(operational_health, "schedules", lambda: {})
    receipt = tmp_path / "state/recovery_receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps(
            {
                "created_at": (now + timedelta(hours=offset_hours)).isoformat(),
                "result": "data_restore_verified",
                "offhost_verified": True,
                "protected_recovery_verified": True,
            }
        )
    )
    async with get_session() as session:
        report = await build_operations(session, now=now)
    assert report["backup_verified"] is expected
