"""Tests for the inbound pipeline + scrape throttle."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
from src.db.models import (
    Base,
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SourceChannel,
)
from src.scrapers.orchestrator import (
    _last_scrape_marker_path,
    scrape_all_with_throttle,
)


@pytest_asyncio.fixture
async def db(tmp_path: Path, monkeypatch):
    """In-memory DB + data_dir pointed at tmp_path so scrape marker is isolated."""
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    orig_engine = db_session._engine
    orig_factory = db_session._session_factory
    db_session._engine = engine
    db_session._session_factory = factory
    try:
        yield factory
    finally:
        await engine.dispose()
        db_session._engine = orig_engine
        db_session._session_factory = orig_factory


@pytest.mark.asyncio
async def test_scrape_throttle_skips_within_window(db, tmp_path):
    """Two calls in quick succession: first runs, second is throttled."""
    with patch(
        "src.scrapers.orchestrator.scrape_platform", new=AsyncMock(return_value=None)
    ):
        ran1, _ = await scrape_all_with_throttle(throttle_minutes=15)
        ran2, _ = await scrape_all_with_throttle(throttle_minutes=15)
    assert ran1 is True
    assert ran2 is False
    assert _last_scrape_marker_path().exists()


@pytest.mark.asyncio
async def test_scrape_throttle_runs_after_window(db, tmp_path):
    """If the marker is older than the window, scrape runs again."""
    with patch(
        "src.scrapers.orchestrator.scrape_platform", new=AsyncMock(return_value=None)
    ):
        await scrape_all_with_throttle(throttle_minutes=15)
        marker = _last_scrape_marker_path()
        # backdate the marker by 20 minutes
        old = time.time() - 20 * 60
        import os
        os.utime(marker, (old, old))
        ran, _ = await scrape_all_with_throttle(throttle_minutes=15)
    assert ran is True


@pytest.mark.asyncio
async def test_mark_stale_inactive_flips_old_listings(db):
    """Only repeated successful full crawls confirm absence; observed listings stay active."""
    from datetime import datetime, timedelta, timezone

    from src.scrapers.orchestrator import mark_stale_inactive

    factory = db
    async with factory() as s:
        old_seen = datetime.now(timezone.utc) - timedelta(days=15)
        recent_seen = datetime.now(timezone.utc) - timedelta(days=2)
        s.add_all([
            Listing(
                platform=Platform.NEKRETNINE, external_id="stale1",
                url="https://x/1", photo_urls=[], photo_phashes=[],
                last_seen_at=old_seen,
            ),
            Listing(
                platform=Platform.NEKRETNINE, external_id="fresh1",
                url="https://x/2", photo_urls=[], photo_phashes=[],
                last_seen_at=recent_seen,
            ),
            Listing(
                platform=Platform.CETIRI_ZIDA, external_id="stale2",
                url="https://x/3", photo_urls=[], photo_phashes=[],
                last_seen_at=old_seen,
            ),
        ])
        await s.commit()

    from src.scrapers.orchestrator import ScrapeResult
    t0 = datetime.now(timezone.utc)
    total_marked = 0
    for platform, observed in [(Platform.NEKRETNINE, {"fresh1"}), (Platform.CETIRI_ZIDA, set())]:
        for week in range(2):
            evidence = ScrapeResult(platform=platform, run_id=f"{platform.value}-{week}", full_crawl=True,
                crawl_complete=True, stop_reason="exhausted", pages_attempted=1,
                denominator_trusted=True, site_total=len(observed), previewed=len(observed),
                fetched=len(observed), observed_ids=observed)
            result = await mark_stale_inactive(source_result=evidence, now=t0+timedelta(days=7*week))
            total_marked += result.marked_inactive
    assert total_marked == 2

    async with factory() as s:
        from sqlalchemy import select
        rows = (await s.execute(select(Listing).order_by(Listing.external_id))).scalars().all()
        by_id = {r.external_id: r for r in rows}
        assert by_id["stale1"].is_active is False
        assert by_id["fresh1"].is_active is True
        assert by_id["stale2"].is_active is False


@pytest.mark.asyncio
async def test_mark_stale_platform_scoped(db):
    """Platform filter only affects that platform."""
    from datetime import datetime, timedelta, timezone

    from src.scrapers.orchestrator import mark_stale_inactive

    factory = db
    async with factory() as s:
        old = datetime.now(timezone.utc) - timedelta(days=15)
        s.add_all([
            Listing(platform=Platform.NEKRETNINE, external_id="ne1",
                    url="https://x/1", photo_urls=[], photo_phashes=[],
                    last_seen_at=old),
            Listing(platform=Platform.CETIRI_ZIDA, external_id="cz1",
                    url="https://x/2", photo_urls=[], photo_phashes=[],
                    last_seen_at=old),
        ])
        await s.commit()

    from src.scrapers.orchestrator import ScrapeResult
    t0 = datetime.now(timezone.utc)
    for week in range(2):
        evidence = ScrapeResult(platform=Platform.NEKRETNINE, run_id=f"week-{week}", full_crawl=True,
            crawl_complete=True, stop_reason="exhausted", pages_attempted=1,
            denominator_trusted=True, site_total=0)
        result = await mark_stale_inactive(source_result=evidence, platform=Platform.NEKRETNINE,
                                          now=t0+timedelta(days=7*week))
    assert result.marked_inactive == 1

    async with factory() as s:
        from sqlalchemy import select
        rows = (await s.execute(select(Listing))).scalars().all()
        by_id = {r.external_id: r for r in rows}
        assert by_id["ne1"].is_active is False
        assert by_id["cz1"].is_active is True  # other platform untouched


@pytest.mark.asyncio
async def test_process_one_reactivates_inactive_listing(db):
    """Re-seeing a listing that was swept inactive must flip is_active back to True (and not
    re-insert it). Without this, a listing wrongly deactivated during a scrape outage stays
    invisible to the matcher (matcher only queries is_active=True) even after it reappears."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from src.scrapers.orchestrator import _process_one

    factory = db
    old = datetime.now(timezone.utc) - timedelta(days=30)
    async with factory() as s:
        s.add(
            Listing(
                platform=Platform.NEKRETNINE, external_id="123",
                url="https://www.nekretnine.rs/oglasi/123/",
                photo_urls=[], photo_phashes=[],
                last_seen_at=old, is_active=False,
            )
        )
        await s.commit()

    scraper = MagicMock()
    scraper.platform = Platform.NEKRETNINE
    # Existing-row branch must return before fetching detail.
    scraper.fetch_detail = AsyncMock(side_effect=AssertionError("must not fetch_detail for existing row"))

    inserted, photos, fetched_detail = await _process_one(
        scraper, "https://www.nekretnine.rs/oglasi/123/", "123", download_photos=False
    )
    assert inserted is False, "reactivation is a refresh, not a new insert (don't inflate 'new')"
    assert photos == 0
    assert fetched_detail is False, "re-seen row takes the no-fetch branch — not a parse attempt"

    async with factory() as s:
        row = (await s.execute(select(Listing).where(Listing.external_id == "123"))).scalar_one()
        assert row.is_active is True
        assert row.last_seen_at.replace(tzinfo=None) > old.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_pipeline_matches_and_calls_delivery(db):
    """process_new_inbound triggers scrape→match→deliver. We mock scrape and delivery."""
    from src.jobs.pipeline import process_new_inbound

    factory = db
    async with factory() as s:
        ch = SourceChannel(tg_id=-1, username="t", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id="x",
            url="https://nekretnine.rs/x",
            photo_urls=["u1", "u2"],
            photo_phashes=["0" * 16, "1" * 16],
        )
        ib = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=1,
            photo_paths=["a.jpg", "b.jpg"],
            photo_phashes=["0" * 16, "1" * 16],
            status=InboundStatus.NEW,
        )
        s.add_all([listing, ib])
        await s.commit()
        iid = ib.id

    mock_client = object()  # we patch delivery so client isn't actually used
    with patch(
        "src.jobs.pipeline.scrape_all_with_throttle",
        new=AsyncMock(return_value=(False, [])),
    ) as mock_scrape, patch(
        "src.jobs.pipeline.send_match_to_saved", new=AsyncMock()
    ) as mock_deliver:
        result = await process_new_inbound(iid, client=mock_client)

    assert mock_scrape.await_count == 1
    assert result.status == "matched"
    assert mock_deliver.await_count == 1
    # delivery was called with the match_id and our client
    call = mock_deliver.await_args
    assert call.args[0] == result.match_id
    assert call.args[1] is mock_client


@pytest.mark.asyncio
async def test_pipeline_no_match_does_not_deliver(db):
    """No match → no bot delivery, but telemetry notify still fires so we can see liveness."""
    from src.jobs.pipeline import process_new_inbound

    factory = db
    async with factory() as s:
        ch = SourceChannel(tg_id=-1, username="t", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id="x",
            url="https://nekretnine.rs/x",
            photo_urls=["u"],
            photo_phashes=["f" * 16],
        )
        ib = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=1,
            photo_paths=["a.jpg"],
            photo_phashes=["0" * 16],
            status=InboundStatus.NEW,
        )
        s.add_all([listing, ib])
        await s.commit()
        iid = ib.id

    with patch(
        "src.jobs.pipeline.scrape_all_with_throttle",
        new=AsyncMock(return_value=(False, [])),
    ), patch(
        "src.jobs.pipeline.send_match_to_saved", new=AsyncMock()
    ) as mock_deliver, patch(
        "src.jobs.pipeline.notify_no_match", new=MagicMock()
    ) as mock_notify:
        result = await process_new_inbound(iid, client=object())

    assert result.status == "no_match"
    assert mock_deliver.await_count == 0  # bot delivery is matched-only
    assert mock_notify.call_count == 1  # telemetry fires on no_match (sync wrapper, returns Task)
    call = mock_notify.call_args
    assert call.args[0] == iid
    assert call.kwargs["best_count"] == result.matched_photos
    assert call.kwargs["method"] == result.method


@pytest.mark.asyncio
async def test_pipeline_matched_skips_no_match_notify(db):
    """Matched inbound → no telemetry notify (bot delivery is the signal)."""
    from src.jobs.pipeline import process_new_inbound

    factory = db
    async with factory() as s:
        ch = SourceChannel(tg_id=-1, username="t", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id="x",
            url="https://nekretnine.rs/x",
            photo_urls=["u1", "u2"],
            photo_phashes=["0" * 16, "1" * 16],
        )
        ib = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=1,
            photo_paths=["a.jpg", "b.jpg"],
            photo_phashes=["0" * 16, "1" * 16],
            status=InboundStatus.NEW,
        )
        s.add_all([listing, ib])
        await s.commit()
        iid = ib.id

    with patch(
        "src.jobs.pipeline.scrape_all_with_throttle",
        new=AsyncMock(return_value=(False, [])),
    ), patch(
        "src.jobs.pipeline.send_match_to_saved", new=AsyncMock()
    ), patch(
        "src.jobs.pipeline.notify_no_match", new=MagicMock()
    ) as mock_notify:
        result = await process_new_inbound(iid, client=object())

    assert result.status == "matched"
    assert mock_notify.call_count == 0
