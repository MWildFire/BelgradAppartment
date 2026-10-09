"""Bounded public cadence without private selection or transport notifications."""

import re
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
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
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def public_timer_slots():
    text = (DEPLOY / "otz-public-detail-refresh.timer").read_text()
    match = re.search(r"^OnCalendar=\*-\*-\* ([\d,]+):(\d\d):00 UTC$", text, re.M)
    assert match, "public cadence must expose its fixed UTC daily slots"
    return [int(hour) * 60 + int(match[2]) for hour in match[1].split(",")]


def unit_seconds(name, setting):
    text = (DEPLOY / name).read_text()
    value = re.search(rf"^{setting}=(.*)$", text, re.M)[1]
    factors = {"h": 3600, "min": 60, "s": 1}
    return sum(int(amount) * factors[unit] for amount, unit in re.findall(r"(\d+)(h|min|s)", value))


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
            succeeded_at=NOW - timedelta(hours=9, minutes=59),
        ),
        NOW,
    )
    assert is_due(
        RefreshCandidate(3, row.platform, row.url, public=True,
                         succeeded_at=NOW - timedelta(hours=10)),
        NOW,
    )
    assert not is_due(
        RefreshCandidate(
            4, row.platform, row.url, public=True, next_retry_at=NOW + timedelta(minutes=1)
        ),
        NOW,
    )


def test_variable_nightly_duration_cannot_skip_a_public_day():
    # A slow nightly finished at 08:00, but the next nightly selects at 03:30.
    # The former 20-hour gate skipped that next pass and waited until 15:00 (31h old).
    row = RefreshCandidate(
        1, Platform.NEKRETNINE, "https://example.test/1", public=True,
        succeeded_at=datetime(2026, 9, 8, 8, tzinfo=UTC),
    )
    assert is_due(row, datetime(2026, 9, 9, 3, 30, tzinfo=UTC))


def test_successful_public_cadence_covers_every_last_success_time_within_24h():
    from src.config import Settings

    midnight = NOW.replace(hour=0, minute=0)
    jitter = unit_seconds("otz-public-detail-refresh.timer", "RandomizedDelaySec")
    budget = Settings(_env_file=None).public_detail_refresh_budget_sec
    slots = [
        midnight + timedelta(days=day, minutes=minute)
        for day in range(4)
        for minute in public_timer_slots()
    ]
    # Exercise arbitrary success times, including manual/variable nightly completion.
    # All public work must fit its configured cap/budget and upstream requests must succeed;
    # this is a scheduling proof, not evidence of real source throughput or attainment.
    for minute in range(0, 24 * 60, 5):
        succeeded = midnight + timedelta(minutes=minute)
        row = RefreshCandidate(
            1, Platform.NEKRETNINE, "https://example.test/1", public=True,
            succeeded_at=succeeded,
        )
        # Eligibility at the earliest possible start, completion at the latest possible
        # start plus full budget: jitter cannot make this bound look artificially better.
        selected = next(slot for slot in slots if slot > succeeded and is_due(row, slot))
        assert selected + timedelta(seconds=jitter + budget) - succeeded <= timedelta(hours=24)


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


async def test_all_inventory_refresh_includes_recent_rows_beyond_quota_but_respects_backoff(
    db, monkeypatch, tmp_path,
):
    import json

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    now = datetime.now(UTC)
    recent = await seed("recent", details_succeeded_at=now, photo_phashes=["0123456789abcdef"])
    other = await seed("recent-other", Platform.CITYEXPERT,
                       details_succeeded_at=now, photo_phashes=["0123456789abcdef"])
    retained = await seed("retained-gone", public=True, is_active=False)
    await seed("backoff", details_next_retry_at=now + timedelta(hours=2))
    await seed("private-inactive", is_active=False)
    captured = []

    async def refetch(rows, **kwargs):
        captured.extend(rows)
        assert kwargs["checkpoint_name"] == "refetch"
        return orch.DetailRefetchStats(selected=len(rows), updated=len(rows), verified=len(rows))

    monkeypatch.setattr(orch, "refetch_listing_details", refetch)
    result = await orch.rolling_detail_refresh(limit=1, all_listings=True)
    assert {row.id for row in captured} == {recent, other, retained}
    assert result.selected == result.refreshed == 3
    assert result.inventory_count == 4
    assert result.retry_deferred == 1
    cursor = json.loads((tmp_path / "state/freshness/rolling_cursor.json").read_text())
    assert cursor["selection_mode"] == "all_inventory"
    assert cursor["inventory_count"] == cursor["selected"] + cursor["retry_deferred"]
    assert cursor["deferred"] == 0


async def test_all_inventory_cannot_expand_public_only_scope(monkeypatch):
    selection = AsyncMock()
    monkeypatch.setattr(orch, "_freshness_queue", selection)
    with pytest.raises(ValueError, match="cannot be public-only"):
        await orch.rolling_detail_refresh(public_only=True, all_listings=True)
    selection.assert_not_called()


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
    def service_timeout(name):
        return unit_seconds(f"{name}.service", "TimeoutStartSec")

    nightly = service_timeout("otz-nightly-refresh")
    weekly = service_timeout("otz-weekly-full-refresh")
    public = service_timeout("otz-public-detail-refresh")
    assert nightly >= settings.scrape_run_budget_sec + 600
    assert weekly >= settings.scrape_run_budget_sec + 600
    assert public >= settings.public_detail_refresh_budget_sec + 300
    # Include the full main-process hard timeout and all randomized delay. Eight days
    # cover both Sunday weekly replacement and the last public slot crossing midnight.
    windows = []
    for day in range(8):
        main = ("otz-weekly-full-refresh", [1260]) if day % 7 == 6 else (
            "otz-nightly-refresh", [1260]
        )
        for name, slots in [
            main, ("otz-scrape-smoke", [720, 1200]),
            ("otz-public-detail-refresh", public_timer_slots()),
        ]:
            jitter = unit_seconds(f"{name}.timer", "RandomizedDelaySec")
            for slot in slots:
                start = day * 86400 + slot * 60
                windows.append((start, start + jitter + service_timeout(name), name))
    ordered = sorted(windows)
    for earlier, later in pairwise(ordered):
        assert earlier[1] < later[0], f"scheduled main windows overlap: {earlier} / {later}"
    text = (DEPLOY / "otz-public-detail-refresh.service").read_text()
    assert "--public-only" in text and "ship-log" not in text and "OnFailure" not in text
    for name in ("otz-weekly-full-refresh", "otz-scrape-smoke"):
        assert "playwright install" not in (DEPLOY / f"{name}.service").read_text()


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
