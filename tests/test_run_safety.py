"""Regression evidence for September 2026 source outcomes and fail-closed sweep."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from src.db import get_session
from src.db.models import Listing, Platform
from src.scrapers.base import ListingDetail, ListingPreview
from src.scrapers.orchestrator import ScrapeResult, mark_stale_inactive, scrape_platform


class FakeScraper:
    platform = Platform.NEKRETNINE
    _crawl_complete = True
    _crawl_stop_reason = "exhausted"
    _pages_attempted = 4
    _pages_failed = 0
    _skipped_pages = 0
    _site_total = 100
    _site_total_trusted = True

    def __init__(self, *, fail=False):
        self.fail = fail

    async def list_recent(self, **kwargs):
        return [
            ListingPreview(
                platform=self.platform, external_id=str(i), url=f"https://example.test/{i}"
            )
            for i in range(100)
        ]

    async def fetch_detail(self, url):
        if self.fail:
            raise RuntimeError("fake item failure")
        return ListingDetail(
            platform=self.platform,
            external_id=url.rsplit("/", 1)[-1],
            url=url,
            title="Flat",
            price_eur=500,
        )


@pytest.mark.asyncio
async def test_all_item_failures_are_failed_and_cannot_sweep(db):
    result = await scrape_platform(FakeScraper(fail=True), incremental=False, download_photos=False)
    assert result.previewed == 100
    assert result.errors == 100
    assert result.details_attempted == 100 and result.parse_rate == 0
    assert result.outcome == "failed"
    assert not result.sweep_allowed
    assert result.run_id


@pytest.mark.asyncio
async def test_old_rows_without_verified_crawl_are_preserved(db):
    async with get_session() as session:
        session.add(
            Listing(
                platform=Platform.NEKRETNINE,
                external_id="old",
                url="https://example.test/old",
                photo_urls=[],
                photo_phashes=[],
                last_seen_at=datetime.now(UTC) - timedelta(days=30),
            )
        )
    report = await mark_stale_inactive(inactive_after_days=3, platform=Platform.NEKRETNINE)
    assert report.marked_inactive == 0
    assert report.blocked_reasons
    async with get_session() as session:
        assert (await session.execute(select(Listing.is_active))).scalar_one()


@pytest.mark.asyncio
async def test_scrape_evaluation_has_no_db_mutations_or_photo_downloads(db, monkeypatch):
    import src.scrapers.orchestrator as orch

    def prohibit_write(conn, cursor, statement, parameters, context, executemany):
        assert (
            not statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        ), statement

    event.listen(db.sync_engine, "before_cursor_execute", prohibit_write)

    async def forbidden(*args, **kwargs):
        pytest.fail("dry-run attempted photo/persistence work")

    monkeypatch.setattr(orch, "_process_one", forbidden)
    monkeypatch.setattr(orch, "_download_photos", forbidden)
    result = await scrape_platform(FakeScraper(), incremental=False, dry_run=True)
    assert result.fetched == 100
    assert result.outcome == "ok"
    assert result.inserted == result.updated == result.photos_downloaded == 0


def complete_result(**overrides):
    values = dict(
        platform=Platform.NEKRETNINE,
        full_crawl=True,
        crawl_complete=True,
        stop_reason="exhausted",
        pages_attempted=4,
        site_total=100,
        denominator_trusted=True,
        previewed=100,
        fetched=100,
        observed_ids={str(i) for i in range(100)},
    )
    values.update(overrides)
    return ScrapeResult(**values)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"previewed": 60, "fetched": 60}, "coverage_mismatch"),
        ({"skipped_pages": 1}, "skipped_pages"),
        ({"pages_failed": 1}, "failed_pages"),
        ({"crawl_complete": False, "stop_reason": "challenge"}, "crawl_incomplete"),
        ({"crawl_complete": False, "stop_reason": "pagination_mismatch"}, "crawl_incomplete"),
        ({"site_total": None}, "untrusted_denominator"),
        ({"denominator_trusted": False}, "untrusted_denominator"),
        ({"fetched": 99}, "items_unprocessed"),
        ({"observed_ids": set()}, "missing_presence_evidence"),
    ],
)
def test_partial_or_untrusted_source_never_permits_sweep(changes, reason):
    result = complete_result(**changes)
    assert result.outcome == "degraded"
    assert not result.sweep_allowed
    assert reason in result.sweep_blocked_reasons


@pytest.mark.asyncio
async def test_failed_list_returns_explicit_source_result(db):
    class Broken(FakeScraper):
        async def list_recent(self, **kwargs):
            raise RuntimeError("test failure")

    result = await scrape_platform(Broken(), incremental=False)
    assert result.platform == Platform.NEKRETNINE
    assert result.outcome == "failed"
    assert "list_exception" in result.sweep_blocked_reasons
    assert result.to_report()["processed"] == 0
    assert "observed_ids" not in result.to_report()


@pytest.mark.asyncio
async def test_weekly_absence_requires_two_distinct_successful_checks(db):
    t0 = datetime(2026, 9, 8, tzinfo=UTC)
    async with get_session() as session:
        session.add(
            Listing(
                platform=Platform.NEKRETNINE,
                external_id="missing",
                url="https://example.test/missing",
                photo_urls=[],
                photo_phashes=[],
                last_seen_at=t0 - timedelta(days=30),
            )
        )
    first = complete_result(run_id="week1")
    report = await mark_stale_inactive(source_result=first, now=t0)
    assert report.marked_inactive == 0
    assert report.absence_recorded == 1
    # Replaying the exact same run is not a second confirmation, even a week later.
    assert (
        await mark_stale_inactive(source_result=first, now=t0 + timedelta(days=7))
    ).marked_inactive == 0
    assert (
        await mark_stale_inactive(
            source_result=complete_result(run_id="too-soon"), now=t0 + timedelta(days=1)
        )
    ).marked_inactive == 0
    second = await mark_stale_inactive(
        source_result=complete_result(run_id="week2"), now=t0 + timedelta(days=7)
    )
    assert second.marked_inactive == 1
    async with get_session() as session:
        row = (await session.execute(select(Listing))).scalar_one()
        assert not row.is_active and row.absence_streak == 2 and row.deactivated_at


@pytest.mark.asyncio
async def test_sweep_evaluation_cannot_mutate_absence_evidence(db):
    async with get_session() as session:
        session.add(
            Listing(
                platform=Platform.NEKRETNINE,
                external_id="missing",
                url="https://example.test/missing",
                photo_urls=[],
                photo_phashes=[],
            )
        )

    def prohibit_write(conn, cursor, statement, parameters, context, executemany):
        assert (
            not statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        ), statement

    event.listen(db.sync_engine, "before_cursor_execute", prohibit_write)
    report = await mark_stale_inactive(source_result=complete_result(dry_run=True), dry_run=True)
    assert report.absence_recorded == 1 and report.marked_inactive == 0
    blocked = await mark_stale_inactive(source_result=complete_result(dry_run=True))
    assert blocked.blocked_reasons == ("evaluation_result_cannot_mutate",)


def test_observation_and_missed_week_reset_absence():
    from src.scrapers.run_safety import evaluate_absence

    t0 = datetime(2026, 9, 8, tzinfo=UTC)
    args = dict(
        streak=1,
        first_seen_at=t0,
        last_seen_at=t0,
        last_run_id="week1",
        run_id="later",
        now=t0 + timedelta(days=14),
    )
    gap = evaluate_absence(observed=False, **args)
    assert gap.streak == 1 and not gap.deactivate
    seen = evaluate_absence(observed=True, **args)
    assert seen.streak == 0 and seen.last_run_id is None


@pytest.mark.asyncio
async def test_source_deactivation_cap_blocks_entire_mutation(db):
    t0 = datetime(2026, 9, 8, tzinfo=UTC)
    async with get_session() as session:
        for idx in range(2):
            session.add(
                Listing(
                    platform=Platform.NEKRETNINE,
                    external_id=f"missing-{idx}",
                    url=f"https://example.test/missing-{idx}",
                    photo_urls=[],
                    photo_phashes=[],
                    absence_streak=1,
                    absence_first_seen_at=t0,
                    absence_last_seen_at=t0,
                    absence_last_run_id="week1",
                )
            )
    report = await mark_stale_inactive(
        source_result=complete_result(run_id="week2"),
        now=t0 + timedelta(days=7),
        max_deactivations=1,
    )
    assert report.would_mark_inactive == 2 and report.marked_inactive == 0
    assert report.blocked_reasons == ("deactivation_cap_exceeded",)
    async with get_session() as session:
        rows = (await session.execute(select(Listing))).scalars().all()
        assert all(row.is_active and row.absence_streak == 1 for row in rows)


def test_weekly_completion_jitter_still_confirms_absence():
    from src.scrapers.run_safety import evaluate_absence

    t0 = datetime(2026, 9, 6, 6, 58, tzinfo=UTC)
    result = evaluate_absence(
        observed=False,
        streak=1,
        first_seen_at=t0,
        last_seen_at=t0,
        last_run_id="week1",
        run_id="week2",
        now=t0 + timedelta(days=7, minutes=-8),
    )
    assert result.deactivate and result.streak == 2


@pytest.mark.asyncio
async def test_hanging_source_returns_failure_checkpoint_and_next_source_runs(db, tmp_path):
    import asyncio
    import json
    class Hanging(FakeScraper):
        async def list_recent(self, **kwargs):
            await asyncio.sleep(5)
    path = tmp_path / "first.json"
    first = await scrape_platform(Hanging(), incremental=False, source_budget_sec=0.01, checkpoint_path=path)
    assert first.outcome == "failed" and first.stop_reason == "source_timeout"
    assert json.loads(path.read_text())["final"] is True
    second = await scrape_platform(FakeScraper(), incremental=False, download_photos=False, checkpoint_path=tmp_path / "second.json")
    assert second.outcome == "ok"


@pytest.mark.asyncio
async def test_source_cancellation_writes_final_checkpoint(db, tmp_path):
    import asyncio
    import json
    class Hanging(FakeScraper):
        async def list_recent(self, **kwargs):
            await asyncio.sleep(5)
    path = tmp_path / "cancelled.json"
    scraper = Hanging()
    task = asyncio.create_task(scrape_platform(scraper, incremental=False, checkpoint_path=path))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = json.loads(path.read_text())
    assert report["final"] and report["stop_reason"] == "cancelled"
    assert not report["sweep_allowed"]
    assert scraper.last_scrape_result.stop_reason == "cancelled"
    assert scraper.last_scrape_result.to_report()["run_id"] == report["run_id"]


@pytest.mark.asyncio
async def test_dry_source_timeout_writes_no_checkpoint(db, tmp_path):
    import asyncio
    class Hanging(FakeScraper):
        async def list_recent(self, **kwargs):
            await asyncio.sleep(5)
    path = tmp_path / "dry.json"
    result = await scrape_platform(Hanging(), dry_run=True, source_budget_sec=0.01, checkpoint_path=path)
    assert result.stop_reason == "source_timeout" and not path.exists()


def test_probe_sweep_broken_only_when_probes_failed_wholesale():
    from src.scrapers.run_safety import probe_sweep_broken

    assert not probe_sweep_broken(checked=0, failed=0)
    assert probe_sweep_broken(checked=0, failed=1)  # errored before any probe ran
    assert not probe_sweep_broken(checked=5, failed=5)  # a tiny sweep: one failure is noise
    assert probe_sweep_broken(checked=10, failed=10)  # every probe failed
    assert not probe_sweep_broken(checked=10, failed=5)  # exactly at the threshold
    assert probe_sweep_broken(checked=10, failed=6)
    assert not probe_sweep_broken(checked=100, failed=1)  # ordinary noise


def test_source_removal_alarm_needs_a_real_sample():
    from src.scrapers.run_safety import source_removal_alarm

    assert not source_removal_alarm(deactivated=0, attempted=50)
    assert not source_removal_alarm(deactivated=1, attempted=2)  # one 404 in a tiny run
    assert not source_removal_alarm(deactivated=3, attempted=9)  # below the sample floor
    assert not source_removal_alarm(deactivated=9, attempted=9)  # tiny set: silent by design
    assert not source_removal_alarm(deactivated=3, attempted=10)  # exactly 0.3 is not above
    assert source_removal_alarm(deactivated=4, attempted=10)


def test_source_removal_alarm_only_above_the_removal_share():
    from src.scrapers.run_safety import source_removal_alarm

    assert not source_removal_alarm(deactivated=0, attempted=10)
    assert not source_removal_alarm(deactivated=3, attempted=10)  # exactly at the threshold
    assert source_removal_alarm(deactivated=4, attempted=10)
    assert source_removal_alarm(deactivated=9, attempted=10)  # what a changed URL scheme looks like
