"""Bounded public cadence without private selection or transport notifications."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

import src.cli as commands
from src.config import get_settings
from src.db import Listing, get_session
from src.db.models import InboundPost, Match, Platform, SiteListing, SourceChannel
from src.scrapers import orchestrator as orch
from src.scrapers.freshness import RefreshCandidate, is_due, load_candidates

NOW = datetime(2026, 9, 8, 15, tzinfo=UTC)


def test_public_due_ahead_of_daily_jitter_but_preserves_retry_backoff():
    row = RefreshCandidate(
        1,
        Platform.NEKRETNINE,
        "https://example.test/1",
        public=True,
        succeeded_at=NOW - timedelta(hours=23, minutes=50),
    )
    assert is_due(row, NOW)
    assert not is_due(
        RefreshCandidate(
            2,
            row.platform,
            row.url,
            public=True,
            succeeded_at=NOW - timedelta(hours=19, minutes=59),
        ),
        NOW,
    )
    assert not is_due(
        RefreshCandidate(
            3, row.platform, row.url, public=True, next_retry_at=NOW + timedelta(minutes=1)
        ),
        NOW,
    )


async def seed(ext, platform=Platform.NEKRETNINE, public=False, **values):
    async with get_session() as session:
        listing = Listing(
            platform=platform, external_id=ext, url=f"https://example.test/{ext}", **values
        )
        session.add(listing)
        await session.flush()
        if public:
            source = SourceChannel(tg_id=listing.id)
            session.add(source)
            await session.flush()
            post = InboundPost(source_channel_id=source.id, tg_message_id=1)
            session.add(post)
            await session.flush()
            match = Match(inbound_post_id=post.id, listing_id=listing.id)
            session.add(match)
            await session.flush()
            session.add(SiteListing(match_id=match.id, is_public=True))
        return listing.id


async def test_public_inventory_includes_retained_original_but_not_private(db):
    retained = await seed("retained", public=True, is_active=False)
    active = await seed("public", public=True)
    await seed("private")
    async with get_session() as session:
        rows = await load_candidates(session, public_only=True)
    assert {row.id for row in rows} == {retained, active}
    assert all(row.public for row in rows)


async def test_public_queue_keeps_source_fairness_backoff_and_own_cursor(db, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    first = await seed("one", public=True)
    second = await seed("two", Platform.CETIRI_ZIDA, public=True)
    await seed("private")
    await seed("retry", public=True, details_next_retry_at=datetime.now(UTC) + timedelta(days=1))
    captured = []

    async def refetch(rows, **kwargs):
        captured.extend(rows)
        assert kwargs["checkpoint_name"] == "public_refetch"
        return orch.DetailRefetchStats(selected=len(rows), updated=len(rows), verified=len(rows))

    monkeypatch.setattr(orch, "refetch_listing_details", refetch)
    result = await orch.rolling_detail_refresh(
        limit=2,
        public_only=True,
        scrapers={Platform.NEKRETNINE: object, Platform.CETIRI_ZIDA: object},
    )
    assert {row.id for row in captured} == {first, second}
    assert result.refreshed == 2
    assert (tmp_path / "state/freshness/public_cursor.json").is_file()
    assert not (tmp_path / "state/freshness/rolling_cursor.json").exists()


def test_public_cli_bounds_queue_and_reports_degraded_without_notifications(monkeypatch):
    refresh = AsyncMock(return_value=orch.RollingRefreshResult(3, 2, 1, []))
    monkeypatch.setattr(orch, "rolling_detail_refresh", refresh)
    result = CliRunner().invoke(
        commands.cli, ["refetch-details", "--public-only", "--limit=3", "--budget-sec=1"]
    )
    assert result.exit_code == 1
    assert "verified=2" in result.output
    kwargs = refresh.call_args.kwargs
    assert kwargs["public_only"] and kwargs["limit"] == 3
    assert kwargs["deadline"] > 0


def test_public_cli_rejects_all_and_text_only_conflicts():
    for flag in ("--all", "--no-photos"):
        result = CliRunner().invoke(commands.cli, ["refetch-details", "--public-only", flag])
        assert result.exit_code == 2
        assert "cannot be combined" in result.output


def test_public_cli_budget_bounds_hanging_queue(monkeypatch):
    import asyncio

    async def hang(**kwargs):
        await asyncio.Future()

    monkeypatch.setattr(orch, "rolling_detail_refresh", hang)
    result = CliRunner().invoke(
        commands.cli, ["refetch-details", "--public-only", "--budget-sec=0.01"]
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, TimeoutError)


@pytest.mark.parametrize("option", ["--limit=0", "--budget-sec=0", "--budget-sec=-1"])
def test_public_cli_rejects_unbounded_or_empty_budget_options(option):
    result = CliRunner().invoke(commands.cli, ["refetch-details", "--public-only", option])
    assert result.exit_code == 2


def test_admin_exposes_new_public_timer(monkeypatch):
    from types import SimpleNamespace

    from src.admin import operational_health

    called = []

    def systemctl(args, **kwargs):
        called.append(args[2])
        return SimpleNamespace(returncode=0, stdout="tomorrow")

    monkeypatch.setattr(operational_health.subprocess, "run", systemctl)
    report = operational_health.schedules()
    assert report["public-detail-refresh"] == "tomorrow"
    assert "otz-public-detail-refresh.timer" in called


def test_configured_budgets_fit_runner_and_scheduled_main_windows():
    import re
    from pathlib import Path

    from src.config import Settings

    settings = Settings(_env_file=None)
    assert settings.scrape_incremental_run_budget_sec >= (
        4 * settings.scrape_incremental_source_budget_sec
        + settings.nightly_refetch_budget_sec
        + 900
    )
    assert settings.scrape_run_budget_sec >= (
        4 * settings.scrape_source_budget_sec + settings.nightly_refetch_budget_sec + 900
    )
    folder = Path(__file__).resolve().parents[1] / "deploy"

    def service_timeout(name):
        text = (folder / f"{name}.service").read_text()
        timeout = re.search(r"^TimeoutStartSec=(.*)$", text, re.M)[1]
        factor = {"h": 3600, "min": 60, "s": 1}
        return sum(
            int(amount) * factor[unit] for amount, unit in re.findall(r"(\d+)(h|min|s)", timeout)
        )

    nightly = service_timeout("otz-nightly-refresh")
    weekly = service_timeout("otz-weekly-full-refresh")
    public = service_timeout("otz-public-detail-refresh")
    assert nightly >= settings.scrape_incremental_run_budget_sec + 600
    assert weekly >= settings.scrape_run_budget_sec + 600
    assert public >= settings.public_detail_refresh_budget_sec + 300
    # Include maximum randomized delay. Main-process timeout windows leave separation
    # from smoke12:30; smoke00:30 (30min + 5min jitter) finishes before weekly02:00.
    assert 3 * 3600 + 15 * 60 + nightly < 12.5 * 3600
    assert 2 * 3600 + 15 * 60 + weekly < 12.5 * 3600
    assert 30 * 60 + 5 * 60 + service_timeout("otz-scrape-smoke") < 2 * 3600
    text = (folder / "otz-public-detail-refresh.service").read_text()
    assert "--public-only" in text and "ship-log" not in text and "OnFailure" not in text
    for name in ("otz-weekly-full-refresh", "otz-scrape-smoke"):
        assert "playwright install" not in (folder / f"{name}.service").read_text()


async def test_public_refresh_writes_verified_success_without_publication_changes(
    db, monkeypatch, tmp_path
):
    from PIL import Image
    from sqlalchemy import func, select

    from src.db.models import MatchDelivery, PublishedPost
    from src.scrapers.base import ListingDetail

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "listings_photos_dir", tmp_path / "listings")
    public_id = await seed("public", public=True, is_active=False)
    private_id = await seed("private")

    class Fake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def fetch_detail(self, url):
            assert url.endswith("/public")
            return ListingDetail(
                platform=Platform.NEKRETNINE,
                external_id="public",
                url=url,
                price_eur=500,
                area_m2=50,
                title="Flat",
                photo_urls=["https://example.test/1.jpg"],
            )

    async def download(detail, *, target_dir):
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "000.jpg"
        Image.new("RGB", (8, 8), "blue").save(path)
        return [path], ["ff00ff00ff00ff00"], None

    monkeypatch.setattr(orch, "_download_photos", download)
    result = await orch.rolling_detail_refresh(
        limit=500,
        public_only=True,
        scrapers={Platform.NEKRETNINE: Fake},
    )
    assert result.selected == result.refreshed == 1
    async with get_session() as session:
        public = await session.get(Listing, public_id)
        private = await session.get(Listing, private_id)
        assert public.details_succeeded_at is not None and public.is_active
        assert private.details_refreshed_at is None and private.details_succeeded_at is None
        assert (await session.execute(select(SiteListing.is_public))).scalar_one() is True
        assert (await session.execute(select(func.count(PublishedPost.id)))).scalar_one() == 0
        assert (await session.execute(select(func.count(MatchDelivery.id)))).scalar_one() == 0
    import json

    checkpoint = json.loads((tmp_path / "state/freshness/public_refetch.json").read_text())
    assert checkpoint["final"] and checkpoint["verified"] == 1 and checkpoint["errors"] == 0
