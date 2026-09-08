"""F08/F09: prove freshness, failure/backoff and source fairness independently of network."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from src.db import get_session
from src.db.models import Listing, Platform
from src.scrapers.freshness import (
    CallbackResult,
    RefreshCandidate,
    RefreshEvidence,
    assess_detail,
    is_due,
    load_candidates,
    quota_estimates,
    record_attempt,
    run_fair_refresh,
    select_fair,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
PLATFORMS = list(Platform)[:3]


def candidate(number, platform=None, **kwargs):
    return RefreshCandidate(
        number, platform or PLATFORMS[0], f"https://example.invalid/{number}", **kwargs
    )


def complete():
    return assess_detail(
        SimpleNamespace(price_eur=600, area_m2=40, photo_urls=["photo"]), ready_photo_count=1
    )


def test_partial_fields_photos_and_invalid_prices_do_not_prove_success():
    assert complete().succeeded
    detail = SimpleNamespace(price_eur=600, area_m2=40, photo_urls=["one", "two"])
    assert not assess_detail(detail, ready_photo_count=1).succeeded
    detail.area_m2 = None
    assert assess_detail(detail, ready_photo_count=2).missing_fields == ("area_m2",)
    detail.area_m2 = 40
    for price in (None, 0, -1, float("nan"), True, "600"):
        detail.price_eur = price
        assert not assess_detail(detail, ready_photo_count=2).succeeded
    detail.price_eur = 600
    detail.photo_urls = []
    assert not assess_detail(detail, ready_photo_count=0).succeeded


@pytest.mark.parametrize(
    "evidence",
    [
        RefreshEvidence.failed("TimeoutError"),
        assess_detail(
            SimpleNamespace(price_eur=600, area_m2=None, photo_urls=["one"]), ready_photo_count=1
        ),
        assess_detail(
            SimpleNamespace(price_eur=600, area_m2=40, photo_urls=["one", "two"]),
            ready_photo_count=1,
        ),
    ],
)
async def test_failed_attempt_never_advances_success_and_retries_back_off(db, evidence):
    async with get_session() as session:
        session.add(
            Listing(
                id=1,
                platform=PLATFORMS[0],
                external_id="fixture",
                url="https://x/1",
                details_succeeded_at=NOW - timedelta(days=10),
            )
        )
    for offset, expected_count in ((0, 1), (2, 2)):
        attempt = NOW + timedelta(hours=offset)
        async with get_session() as session:
            result = await record_attempt(
                session,
                1,
                evidence,
                attempted_at=attempt,
                completed_at=attempt,
            )
            assert result.applied and not result.succeeded
            assert result.failure_count == expected_count
            assert result.next_retry_at == attempt + timedelta(
                minutes=30 * 2 ** (expected_count - 1)
            )
    async with get_session() as session:
        listing = await session.get(Listing, 1)
        assert listing.details_succeeded_at.replace(tzinfo=UTC) == NOW - timedelta(days=10)
        assert listing.details_refreshed_at.replace(tzinfo=UTC) == NOW + timedelta(hours=2)
        assert listing.detail_failure_count == 2
        result = await record_attempt(
            session,
            1,
            complete(),
            attempted_at=NOW + timedelta(hours=3),
            completed_at=NOW + timedelta(hours=3, seconds=10),
        )
        assert result.applied and result.succeeded and result.next_retry_at is None
    async with get_session() as session:
        row = await session.get(Listing, 1)
        assert row.details_succeeded_at.replace(tzinfo=UTC) == NOW + timedelta(hours=3, seconds=10)
        assert row.detail_failure_count == 0


async def test_legacy_attempt_is_not_success_and_dry_run_cannot_write(db):
    async with get_session() as session:
        session.add(
            Listing(
                id=1,
                platform=PLATFORMS[0],
                external_id="fixture",
                url="https://x/1",
                details_refreshed_at=NOW,
            )
        )

    def reject_write(_conn, _cursor, statement, _parameters, _context, _many):
        assert not statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE", "REPLACE"))

    event.listen(db.sync_engine, "before_cursor_execute", reject_write)
    async with get_session() as session:
        inventory = await load_candidates(session)
        assert inventory[0].succeeded_at is None and is_due(inventory[0], NOW)
        result = await record_attempt(
            session, 1, complete(), attempted_at=NOW, completed_at=NOW, dry_run=True
        )
        assert not result.applied
        assert (await session.execute(select(Listing.details_succeeded_at))).scalar_one() is None


async def test_late_failure_cannot_overwrite_newer_success(db):
    async with get_session() as session:
        session.add(Listing(id=1, platform=PLATFORMS[0], external_id="fixture", url="https://x/1"))
    async with get_session() as session:
        await record_attempt(session, 1, complete(), attempted_at=NOW, completed_at=NOW)
    async with get_session() as session:
        old = await record_attempt(
            session,
            1,
            RefreshEvidence.failed("TimeoutError"),
            attempted_at=NOW - timedelta(hours=1),
            completed_at=NOW,
        )
        assert not old.applied
        listing = await session.get(Listing, 1)
        assert listing.detail_failure_count == 0 and listing.details_next_retry_at is None


def test_small_quota_rotates_sources_and_donates_unused_slots():
    rows = [candidate(number + 1, platform) for number, platform in enumerate(PLATFORMS)]
    first = select_fair(rows, limit=2, now=NOW)
    second = select_fair(rows, limit=2, now=NOW, source_cursor=first.next_cursor)
    assert len({row.platform for row in first.rows}) == 2
    assert set(row.platform for row in first.rows + second.rows) == set(PLATFORMS)
    rows.extend([candidate(4, PLATFORMS[0]), candidate(5, PLATFORMS[0])])
    assert len(select_fair(rows, limit=5, now=NOW).rows) == 5


def test_public_broken_old_success_priority_and_backoff():
    rows = [
        candidate(1, succeeded_at=NOW - timedelta(days=20)),
        candidate(2, broken=True),
        candidate(3, public=True),
        candidate(4, public=True, next_retry_at=NOW + timedelta(hours=1)),
        candidate(5, succeeded_at=NOW - timedelta(days=10), attempted_at=NOW),
    ]
    plan = select_fair(rows, limit=5, now=NOW)
    assert [row.id for row in plan.rows] == [3, 2, 1, 5]
    assert not is_due(candidate(6, public=True, succeeded_at=NOW - timedelta(hours=12)), NOW)
    assert is_due(candidate(7, succeeded_at=NOW + timedelta(days=1)), NOW)


async def test_first_hanging_item_respects_other_sources_reserved_time():
    rows = [candidate(number + 1, platform) for number, platform in enumerate(PLATFORMS)]
    plan = select_fair(rows, limit=3, now=NOW)
    visited = []

    async def callback(row, _context):
        visited.append(row.id)
        if row.platform == plan.source_order[0]:
            await asyncio.Event().wait()
        return CallbackResult(complete(), payload=row.id)

    result = await run_fair_refresh(plan, callback, budget_sec=0.15, item_timeout_sec=10)
    assert result.attempted == 3 and result.succeeded == 2
    assert len(visited) == 3
    assert result.items[0].evidence.reason == "TimeoutError"
    assert result.elapsed_sec < 0.5


async def test_hanging_context_startup_cannot_consume_other_sources():
    plan = select_fair([candidate(i + 1, p) for i, p in enumerate(PLATFORMS)], limit=3, now=NOW)
    held = set()

    @asynccontextmanager
    async def context(platform):
        assert not held  # never hold two browser/resource leases at once
        if platform == plan.source_order[0]:
            await asyncio.Event().wait()
        held.add(platform)
        try:
            yield platform
        finally:
            held.remove(platform)

    async def callback(row, source):
        assert row.platform == source
        return CallbackResult(complete())

    result = await run_fair_refresh(
        plan, callback, budget_sec=0.15, item_timeout_sec=10, context_factory=context
    )
    assert result.succeeded == 2 and result.attempted == 2
    assert sum(not item.attempted for item in result.items) == 1
    assert not held


async def test_one_context_reused_per_source_and_failed_item_does_not_abort_source():
    plan = select_fair([candidate(1), candidate(2)], limit=2, now=NOW)
    starts = []

    @asynccontextmanager
    async def context(platform):
        starts.append(platform)
        yield "browser"

    async def callback(row, browser):
        assert browser == "browser"
        if row.id == 1:
            raise RuntimeError("no private exception message in result")
        return CallbackResult(complete())

    result = await run_fair_refresh(
        plan, callback, budget_sec=1, item_timeout_sec=0.1, context_factory=context
    )
    assert starts == [PLATFORMS[0]] and result.succeeded == 1
    assert result.items[0].evidence.reason == "RuntimeError"


def test_quota_estimate_exposes_shortfall_without_guessing_success_rate():
    rows = [candidate(i, public=i < 14, broken=i == 0) for i in range(140)]
    measured = quota_estimates(rows, now=NOW)[PLATFORMS[0].value]
    assert measured["fresh_active_7d"] == measured["fresh_public_24h"] == 0
    assert measured["minimum_daily_successes"] == 31  # 14 daily + (133-14)/7
    assert measured["estimated_daily_attempts"] is None
    measured = quota_estimates(rows, now=NOW, measured_success_rates={PLATFORMS[0]: 0.5})
    assert measured[PLATFORMS[0].value]["estimated_daily_attempts"] == 62
    assert (
        quota_estimates(rows, now=NOW, measured_success_rates={PLATFORMS[0]: 0})[
            PLATFORMS[0].value
        ]["estimated_daily_attempts"]
        is None
    )


async def test_replayed_attempt_does_not_double_failure_count(db):
    async with get_session() as session:
        session.add(Listing(id=1, platform=PLATFORMS[0], external_id="fixture", url="https://x/1"))
    async with get_session() as session:
        first = await record_attempt(
            session, 1, RefreshEvidence.failed("TimeoutError"), attempted_at=NOW, completed_at=NOW
        )
        assert first.applied
    async with get_session() as session:
        replay = await record_attempt(
            session,
            1,
            RefreshEvidence.failed("TimeoutError"),
            attempted_at=NOW,
            completed_at=NOW + timedelta(seconds=2),
        )
        assert not replay.applied
        assert (await session.get(Listing, 1)).detail_failure_count == 1
