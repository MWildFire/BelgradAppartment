"""Tests for per-parser parse-success-rate metrics + degradation alert (B1).

After each scrape we compute, per platform, `parse_rate = details_parsed_ok / details_attempted`.
When the rate falls below `parse_rate_min` (default 0.7) with a large-enough sample
(`details_attempted >= parse_rate_min_sample`), the nightly summary raises a 🚨 line AND a
standalone alert ("parser degrading / markup likely changed"). This is layer 3 of A2 as part of
the normal scrape run.

Counters are in-memory on `ScrapeResult` (no DB migration this phase).

DB setup mirrors tests/test_orchestrator_reseen.py: an in-memory SQLite swapped into the global
session factory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
from src.config import get_settings
from src.db.models import Base, Listing, Platform
from src.scrapers.base import ListingDetail, ListingPreview
from src.scrapers.orchestrator import ScrapeResult, scrape_platform


@pytest_asyncio.fixture
async def factory():
    """In-memory DB swapped into the global session factory; yields the factory."""
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


class _FakeScraper:
    """Fake scraper for scrape_platform: list_recent yields N previews; fetch_detail returns a
    good detail for the first `good` urls and an all-None shell for the rest."""

    platform = Platform.NEKRETNINE

    def __init__(self, total: int, good: int):
        self._total = total
        self._good = good
        self._seen = 0

    async def list_recent(self, *, max_pages, known_ids):
        previews = []
        for i in range(self._total):
            ext = f"id{i}"
            previews.append(
                ListingPreview(
                    platform=self.platform,
                    external_id=ext,
                    url=f"https://nekretnine.example/oglasi/{ext}/",
                )
            )
        return previews

    async def fetch_detail(self, url: str) -> ListingDetail:
        ext = url.rstrip("/").rsplit("/", 1)[-1]
        idx = int(ext.removeprefix("id"))
        if idx < self._good:
            return ListingDetail(
                platform=self.platform,
                external_id=ext,
                url=url,
                price_eur=500,
                title="Stan",
                photo_urls=["https://img.example/1.jpg"],
            )
        # All-None shell — triggers the empty-shell guard in _process_one.
        return ListingDetail(platform=self.platform, external_id=ext, url=url)


class _SnapshotFake:
    platform = Platform.NEKRETNINE

    def __init__(self, snapshot: ListingDetail, detail: ListingDetail | None = None) -> None:
        self.snapshot = snapshot
        self.detail = detail or snapshot
        self.proxy_metrics = {"proxy_challenges": 2, "proxy_failovers": 1, "proxy_errors": 0}
        self.fetches = 0

    async def list_recent(self, *, max_pages, known_ids):
        return [
            ListingPreview(
                platform=self.platform,
                external_id=self.snapshot.external_id,
                url=self.snapshot.url,
                snapshot=self.snapshot,
            )
        ]

    async def fetch_detail(self, url: str) -> ListingDetail:
        self.fetches += 1
        return self.detail


def _complete_snapshot(external_id: str = "snapshot-1", *, address_raw: str | None = "Address") -> ListingDetail:
    return ListingDetail(
        platform=Platform.NEKRETNINE,
        external_id=external_id,
        url=f"https://www.nekretnine.rs/oglasi/{external_id}/",
        price_eur=700,
        area_m2=50.0,
        rooms=2.0,
        district="Vračar",
        address_raw=address_raw,
        lat=44.8,
        lon=20.46,
        title="Flat",
        photo_urls=["https://pic.nekretnine.rs/image/1001/xxl.jpg"],
    )


# --------------------------------------------------------------------------- parse_rate property


def test_parse_rate_property_basic():
    r = ScrapeResult(platform=Platform.NEKRETNINE, details_attempted=10, details_parsed_ok=8)
    assert r.parse_rate == pytest.approx(0.8)


def test_parse_rate_zero_attempts_is_one():
    """No attempts must NOT look like degradation: parse_rate is 1.0 (and the sample gate keeps it
    from ever alerting)."""
    r = ScrapeResult(platform=Platform.NEKRETNINE, details_attempted=0, details_parsed_ok=0)
    assert r.parse_rate == 1.0


def test_cli_summary_includes_snapshot_and_proxy_telemetry(capsys):
    from src.cli import _print_summary

    _print_summary([
        ScrapeResult(
            platform=Platform.NEKRETNINE,
            previewed=10,
            snapshot_mode="active",
            snapshots_seen=10,
            snapshots_complete=9,
            snapshot_fallbacks=1,
            proxy_metrics={"proxy_challenges": 2, "proxy_failovers": 1, "proxy_errors": 0},
        )
    ])

    output = capsys.readouterr().out
    assert "snapshots=9/10" in output
    assert "fallback=1" in output
    assert "proxy(challenge=2,failover=1,errors=0)" in output


# --------------------------------------------------------------------------- counter wiring


@pytest.mark.asyncio
async def test_scrape_counts_parsed_ok_and_shells(factory):
    """8 good + 2 shells out of 10 fetched details → attempted=10, ok=8, empty_shells=2."""
    scraper = _FakeScraper(total=10, good=8)
    result = await scrape_platform(
        scraper, max_pages=1, max_new=None, download_photos=False, incremental=False
    )
    assert result.details_attempted == 10
    assert result.details_parsed_ok == 8
    assert result.empty_shells == 2
    assert result.parse_rate == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_reseen_listings_do_not_inflate_attempts(factory):
    """Re-seen listings (existing rows) take the no-fetch branch in _process_one and must NOT count
    as a detail attempt."""
    # Pre-seed 4 of the 6 previews as existing rows.
    async with factory() as s:
        for i in range(4):
            ext = f"id{i}"
            s.add(
                Listing(
                    platform=Platform.NEKRETNINE,
                    external_id=ext,
                    url=f"https://nekretnine.example/oglasi/{ext}/",
                    photo_urls=[],
                    photo_phashes=[],
                    last_seen_at=datetime.now(timezone.utc) - timedelta(days=1),
                )
            )
        await s.commit()

    scraper = _FakeScraper(total=6, good=6)
    result = await scrape_platform(
        scraper, max_pages=1, max_new=None, download_photos=False, incremental=False
    )
    # Only the 2 genuinely new previews fetch a detail.
    assert result.details_attempted == 2
    assert result.details_parsed_ok == 2
    assert result.updated == 4


@pytest.mark.asyncio
async def test_snapshot_and_proxy_telemetry_is_wired_to_scrape_result(factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "nekretnine_list_snapshot_mode", "active")
    scraper = _SnapshotFake(_complete_snapshot())

    result = await scrape_platform(
        scraper, max_pages=1, max_new=None, download_photos=False, incremental=False
    )

    assert result.snapshot_mode == "active"
    assert result.snapshots_seen == 1
    assert result.snapshots_complete == 1
    assert result.snapshots_incomplete == 0
    assert result.snapshot_active_uses == 1
    assert result.snapshot_fallbacks == 0
    assert result.snapshot_fallback_rate == 0.0
    assert result.details_attempted == 0
    assert result.proxy_metrics == scraper.proxy_metrics
    assert scraper.fetches == 0


@pytest.mark.asyncio
async def test_incomplete_snapshot_telemetry_counts_legacy_fallback(factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "nekretnine_list_snapshot_mode", "active")
    incomplete = _complete_snapshot(address_raw=None)
    scraper = _SnapshotFake(incomplete, detail=_complete_snapshot())

    result = await scrape_platform(
        scraper, max_pages=1, max_new=None, download_photos=False, incremental=False
    )

    assert result.snapshots_seen == 1
    assert result.snapshots_complete == 0
    assert result.snapshots_incomplete == 1
    assert result.snapshot_fallbacks == 1
    assert result.snapshot_fallback_rate == 1.0
    assert result.details_attempted == 1
    assert scraper.fetches == 1


@pytest.mark.asyncio
async def test_shadow_mismatch_count_is_exposed_in_scrape_result(factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "nekretnine_list_snapshot_mode", "shadow")
    snapshot = _complete_snapshot()
    detail = _complete_snapshot()
    detail.price_eur = 725
    scraper = _SnapshotFake(snapshot, detail=detail)

    result = await scrape_platform(
        scraper, max_pages=1, max_new=None, download_photos=False, incremental=False
    )

    assert result.snapshot_mode == "shadow"
    assert result.snapshot_shadow_mismatches == 1
    assert result.snapshot_active_uses == 0
    assert result.details_attempted == 1


# --------------------------------------------------------------------------- degradation guard


def _degradation_alerts(results, *, parse_rate_min=0.7, parse_rate_min_sample=10):
    """Mirror of the nightly guard under test — imported from cli so the test exercises the real
    code path, not a re-implementation."""
    from src.cli import _parse_rate_degraded

    return _parse_rate_degraded(
        results,
        parse_rate_min=parse_rate_min,
        parse_rate_min_sample=parse_rate_min_sample,
    )


def test_no_alert_when_rate_above_threshold():
    r = ScrapeResult(platform=Platform.NEKRETNINE, details_attempted=10, details_parsed_ok=8)
    assert _degradation_alerts([r]) == []


def test_alert_when_rate_below_threshold():
    r = ScrapeResult(platform=Platform.NEKRETNINE, details_attempted=10, details_parsed_ok=3)
    alerts = _degradation_alerts([r])
    assert len(alerts) == 1
    assert Platform.NEKRETNINE.value in alerts[0]
    assert "🚨" in alerts[0]
    assert "3/10" in alerts[0]
    assert "разметка" in alerts[0]


def test_no_alert_when_sample_too_small():
    """Low rate but tiny sample must NOT cry wolf."""
    r = ScrapeResult(platform=Platform.NEKRETNINE, details_attempted=4, details_parsed_ok=1)
    assert _degradation_alerts([r], parse_rate_min_sample=10) == []


def test_no_alert_when_no_attempts():
    """A platform with zero detail fetches (all re-seen) must never alert."""
    r = ScrapeResult(platform=Platform.NEKRETNINE, details_attempted=0, details_parsed_ok=0)
    assert _degradation_alerts([r]) == []
