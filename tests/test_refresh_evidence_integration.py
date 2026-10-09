"""Freshness integration: real local photo evidence and fake source transports."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from src.db import Listing, get_session
from src.db.models import Platform
from src.scrapers import orchestrator as orch
from src.scrapers.base import ListingDetail


@pytest.fixture(autouse=True)
def isolated_media(tmp_path, monkeypatch):
    monkeypatch.setattr(orch.get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(orch.get_settings(), "listings_photos_dir", tmp_path / "listings")


async def seed(ext="1", platform=Platform.NEKRETNINE, **kw):
    async with get_session() as session:
        listing = Listing(
            platform=platform, external_id=ext, url=f"https://example.test/{ext}", **kw
        )
        session.add(listing)
        await session.flush()
        return SimpleNamespace(id=listing.id, platform=platform, url=listing.url)


async def read(row):
    async with get_session() as session:
        return await session.get(Listing, row.id)


def detail(row, **kw):
    values = dict(price_eur=500, area_m2=50, title="Flat", photo_urls=["https://img.test/1.jpg"])
    values.update(kw)
    return ListingDetail(
        platform=row.platform, external_id=row.url.rsplit("/", 1)[-1], url=row.url, **values
    )


def scraper_for(payload):
    class Fake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def fetch_detail(self, url):
            return payload

    return Fake


async def save_photos(payload, *, target_dir=None):
    folder = (
        target_dir
        or orch.get_settings().listings_photos_dir / payload.platform.value / payload.external_id
    )
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, url in enumerate(payload.photo_urls):
        path = folder / f"{index:03d}{orch._guess_extension(url)}"
        Image.new("RGB", (8, 8), "blue").save(path)
        paths.append(path)
    return paths, ["ff00ff00ff00ff00"] * len(paths), None


async def test_complete_fields_without_local_media_never_becomes_fresh(db):
    row = await seed(photo_urls=["https://img.test/1.jpg"], photo_phashes=["ff"])
    stats = await orch.refetch_listing_details(
        [row],
        download_photos=False,
        sleep_between=0,
        scrapers={row.platform: scraper_for(detail(row))},
    )
    stored = await read(row)
    assert stored.details_succeeded_at is None
    assert stored.details_refreshed_at is not None
    assert stored.details_next_retry_at is not None
    assert stored.detail_failure_count == 1
    assert stats.errors == 1


async def test_verified_success_is_atomic_with_content_and_media(db, monkeypatch):
    row = await seed()
    monkeypatch.setattr(orch, "_download_photos", save_photos)
    stats = await orch.refetch_listing_details(
        [row],
        sleep_between=0,
        scrapers={row.platform: scraper_for(detail(row))},
    )
    stored = await read(row)
    assert stored.details_succeeded_at is not None
    assert stored.details_next_retry_at is None
    assert stored.detail_failure_count == 0
    assert stored.photo_phashes == ["ff00ff00ff00ff00"]
    assert stored.price_eur == 500 and stored.area_m2 == 50
    assert stats.verified == 1 and stats.errors == 0


async def test_partial_new_album_preserves_previous_album_bytes_and_metadata(db, monkeypatch):
    row = await seed(
        photo_urls=["https://img.test/old.jpg"],
        photo_phashes=["old"],
        photo_embeddings=b"old",
        price_eur=700,
    )
    folder = orch.get_settings().listings_photos_dir / row.platform.value / "1"
    folder.mkdir(parents=True)
    previous = folder / "000.jpg"
    Image.new("RGB", (8, 8), "red").save(previous)
    old_bytes = previous.read_bytes()

    async def partial(payload, *, target_dir=None):
        paths, hashes, emb = await save_photos(payload, target_dir=target_dir)
        return paths[:1], hashes[:1], emb

    monkeypatch.setattr(orch, "_download_photos", partial)
    payload = detail(row, photo_urls=["https://img.test/new.jpg", "https://img.test/2.jpg"])
    stats = await orch.refetch_listing_details(
        [row],
        sleep_between=0,
        scrapers={row.platform: scraper_for(payload)},
    )
    stored = await read(row)
    assert stored.photo_urls == ["https://img.test/old.jpg"]
    assert stored.photo_phashes == ["old"] and stored.photo_embeddings == b"old"
    assert previous.read_bytes() == old_bytes
    assert stored.details_succeeded_at is None
    assert stored.detail_failure_count == 1
    assert stats.errors == 1 and stats.updated == 0


async def test_cancel_records_attempt_failure_and_final_checkpoint(db):
    row = await seed()
    entered = asyncio.Event()

    class Hanging(scraper_for(None)):
        async def fetch_detail(self, url):
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(
        orch.refetch_listing_details(
            [row],
            sleep_between=0,
            scrapers={row.platform: Hanging},
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await read(row)
    assert stored.details_refreshed_at is not None
    assert stored.details_succeeded_at is None
    assert stored.detail_failure_count == 1
    import json

    report = json.loads((orch.get_settings().data_dir / "state/freshness/refetch.json").read_text())
    assert report["final"] is True and report["stop_reason"] == "cancelled"
    assert report["reason_counts"] == {row.platform.value: {"item": {"cancelled": 1}}}


async def test_hanging_first_source_leaves_time_for_second_source(db, monkeypatch):
    first = await seed("slow", Platform.CETIRI_ZIDA)
    second = await seed("fast", Platform.NEKRETNINE)
    monkeypatch.setattr(orch, "_download_photos", save_photos)
    entered = []

    class Slow(scraper_for(None)):
        async def __aenter__(self):
            entered.append("slow")
            await asyncio.Future()

    class Fast(scraper_for(detail(second))):
        async def __aenter__(self):
            entered.append("fast")
            return self

    stats = await orch.refetch_listing_details(
        [first, second],
        sleep_between=0,
        deadline=time.monotonic() + 0.3,
        scrapers={first.platform: Slow, second.platform: Fast},
    )
    assert entered == ["slow", "fast"]
    assert stats.verified == 1 and stats.skipped == 1
    assert stats.source_errors == {first.platform.value: "TimeoutError"}
    assert (await read(first)).details_refreshed_at is None
    assert (await read(second)).details_succeeded_at is not None


async def test_retry_backoff_and_success_age_control_next_automatic_batch(db, monkeypatch):
    failed = await seed("failed")
    good = await seed("good")
    monkeypatch.setattr(orch, "_download_photos", save_photos)

    class Mixed(scraper_for(None)):
        async def fetch_detail(self, url):
            if url == failed.url:
                raise RuntimeError("temporary")
            return detail(good)

    result = await orch.rolling_detail_refresh(limit=2, scrapers={good.platform: Mixed})
    assert result.refreshed == 1 and result.errors == 1
    assert (await read(failed)).detail_failure_count == 1

    class Forbidden(scraper_for(None)):
        async def __aenter__(self):
            raise AssertionError("fresh and backoff rows must not open a browser")

    next_result = await orch.rolling_detail_refresh(limit=2, scrapers={good.platform: Forbidden})
    assert next_result.selected == 0 and next_result.errors == 0


async def test_rolling_donates_unused_source_slots_and_advances_persistent_cursor(db, monkeypatch):
    first = await seed("only", Platform.CETIRI_ZIDA)
    rest = [await seed(str(index), Platform.NEKRETNINE) for index in range(3)]
    monkeypatch.setattr(orch, "_download_photos", save_photos)
    seen = []
    by_url = {row.url: detail(row) for row in [first, *rest]}

    class All(scraper_for(None)):
        async def fetch_detail(self, url):
            seen.append(url)
            return by_url[url]

    result = await orch.rolling_detail_refresh(
        limit=4,
        scrapers={first.platform: All, rest[0].platform: All},
    )
    assert result.selected == result.refreshed == 4
    assert set(seen) == set(by_url)
    cursor = orch.get_settings().data_dir / "state/freshness/rolling_cursor.json"
    assert cursor.is_file()


async def test_existing_missing_photo_file_is_redownloaded_despite_hash_count(db, monkeypatch):
    row = await seed(photo_urls=["https://img.test/1.jpg"], photo_phashes=["ff"])
    calls = []

    async def download(payload, **kwargs):
        calls.append(payload.url)
        return await save_photos(payload, **kwargs)

    monkeypatch.setattr(orch, "_download_photos", download)
    stats = await orch.refetch_listing_details(
        [row],
        sleep_between=0,
        scrapers={row.platform: scraper_for(detail(row))},
    )
    assert calls == [row.url] and stats.verified == 1


async def test_partial_fields_preserve_known_values_and_do_not_advance_success(db, monkeypatch):
    row = await seed(price_eur=700, area_m2=60, title="Known")
    monkeypatch.setattr(orch, "_download_photos", save_photos)
    payload = detail(row, price_eur=None, area_m2=None, title=None)
    stats = await orch.refetch_listing_details(
        [row],
        sleep_between=0,
        scrapers={row.platform: scraper_for(payload)},
    )
    stored = await read(row)
    assert (stored.price_eur, stored.area_m2, stored.title) == (700, 60, "Known")
    assert stored.details_succeeded_at is None and stored.detail_failure_count == 1
    assert stats.updated == 1 and stats.verified == 0 and stats.errors == 1
    assert stats.price_changes == []


async def test_new_scrape_only_marks_verified_detail_success_with_media(db, monkeypatch):
    monkeypatch.setattr(orch, "_download_photos", save_photos)
    row = SimpleNamespace(platform=Platform.NEKRETNINE, url="https://example.test/new")
    fake = scraper_for(detail(row))()
    fake.platform = row.platform
    await orch._process_one(fake, row.url, "new", download_photos=True)
    async with get_session() as session:
        listing = (await session.execute(select(Listing))).scalar_one()
        assert listing.details_refreshed_at is not None
        assert listing.details_succeeded_at is not None
        assert listing.detail_failure_count == 0


async def test_verified_refetch_reactivates_retained_original_and_resets_absence(db, monkeypatch):
    row = await seed(is_active=False, link_status="DEAD", absence_streak=2)
    monkeypatch.setattr(orch, "_download_photos", save_photos)
    await orch.refetch_listing_details(
        [row],
        sleep_between=0,
        scrapers={row.platform: scraper_for(detail(row))},
    )
    stored = await read(row)
    assert stored.is_active and stored.link_status == "ACTIVE"
    assert stored.link_succeeded_at is not None
    assert stored.absence_streak == 0 and stored.deactivated_at is None


async def test_cancel_after_sql_commit_settles_media_and_records_actual_success(db, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    row = await seed(photo_urls=["https://img.test/old.jpg"], photo_phashes=["old"])
    old_folder = orch.get_settings().listings_photos_dir / row.platform.value / "1"
    old_folder.mkdir(parents=True)
    old_path = old_folder / "000.jpg"
    Image.new("RGB", (8, 8), "red").save(old_path)
    old_bytes = old_path.read_bytes()
    monkeypatch.setattr(orch, "_download_photos", save_photos)
    real_commit = AsyncSession.commit
    sql_committed = asyncio.Event()
    return_from_commit = asyncio.Event()
    delayed_once = False

    async def delayed_commit(session):
        nonlocal delayed_once
        accepted = any(
            isinstance(value, Listing)
            and value.__dict__.get("photo_urls") == ["https://img.test/new.jpg"]
            for value in session.identity_map.values()
        )
        await real_commit(session)
        if accepted and not delayed_once:
            delayed_once = True
            sql_committed.set()
            await return_from_commit.wait()

    monkeypatch.setattr(AsyncSession, "commit", delayed_commit)
    payload = detail(row, photo_urls=["https://img.test/new.jpg"])
    task = asyncio.create_task(
        orch.refetch_listing_details(
            [row],
            sleep_between=0,
            scrapers={row.platform: scraper_for(payload)},
        )
    )
    await asyncio.wait_for(sql_committed.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    return_from_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await read(row)
    assert stored.photo_urls == ["https://img.test/new.jpg"]
    assert stored.details_succeeded_at is not None and stored.detail_failure_count == 0
    assert old_path.read_bytes() != old_bytes
    import json

    state = json.loads((orch.get_settings().data_dir / "state/freshness/refetch.json").read_text())
    assert state["final"] and state["stop_reason"] == "cancelled"
    assert state["verified"] == 1 and state["errors"] == 0
    assert state["reason_counts"] == {}


async def test_source_timeout_during_commit_preserves_success_and_continues_queue(db, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    first = await seed("slow-commit", Platform.CETIRI_ZIDA)
    second = await seed("next-source", Platform.NEKRETNINE)
    monkeypatch.setattr(orch, "_download_photos", save_photos)
    real_commit = AsyncSession.commit
    delayed_once = False
    deadline = time.monotonic() + 3.0  # two equal source shares of 1.5 s each

    async def delayed_commit(session):
        nonlocal delayed_once
        accepted_first = any(
            isinstance(value, Listing)
            and value.__dict__.get("external_id") == "slow-commit"
            and value.__dict__.get("photo_urls") == ["https://img.test/1.jpg"]
            for value in session.identity_map.values()
        )
        await real_commit(session)
        if accepted_first and not delayed_once:
            delayed_once = True
            # Hold until 0.5 s after the first share ends: its timeout then lands after the
            # SQL commit, and the second source keeps the remaining ~1 s of the budget.
            await asyncio.sleep(max(0.0, deadline - 1.0 - time.monotonic()))

    monkeypatch.setattr(AsyncSession, "commit", delayed_commit)
    stats = await orch.refetch_listing_details(
        [first, second],
        sleep_between=0,
        deadline=deadline,
        scrapers={
            first.platform: scraper_for(detail(first)),
            second.platform: scraper_for(detail(second)),
        },
    )
    assert stats.verified == 2 and stats.errors == 0
    assert stats.source_errors == {first.platform.value: "TimeoutError"}
    assert (await read(first)).details_succeeded_at is not None
    assert (await read(second)).details_succeeded_at is not None


async def test_cancel_during_failed_acceptance_still_cancels_remaining_work(db, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    first = await seed("failed-commit", Platform.CETIRI_ZIDA, price_eur=700)
    second = await seed("must-not-fetch", Platform.NEKRETNINE)
    monkeypatch.setattr(orch, "_download_photos", save_photos)
    real_commit = AsyncSession.commit
    accepting = asyncio.Event()
    fail_commit = asyncio.Event()
    failed_once = False

    async def failing_commit(session):
        nonlocal failed_once
        accepting_first = any(
            isinstance(value, Listing)
            and value.__dict__.get("external_id") == "failed-commit"
            and value.__dict__.get("photo_urls") == ["https://img.test/1.jpg"]
            for value in session.identity_map.values()
        )
        if accepting_first and not failed_once:
            failed_once = True
            accepting.set()
            await fail_commit.wait()
            raise RuntimeError("database_error")
        await real_commit(session)

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)
    fetched = []

    class Following(scraper_for(detail(second))):
        async def fetch_detail(self, url):
            fetched.append(url)
            return detail(second)

    task = asyncio.create_task(
        orch.refetch_listing_details(
            [first, second],
            sleep_between=0,
            scrapers={first.platform: scraper_for(detail(first)), second.platform: Following},
        )
    )
    await asyncio.wait_for(accepting.wait(), timeout=2)
    task.cancel()
    fail_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fetched == []
    stored = await read(first)
    assert stored.price_eur == 700 and stored.details_succeeded_at is None
    assert stored.detail_failure_count == 1


@pytest.mark.parametrize("platform,inactive", [
    (Platform.CETIRI_ZIDA, False), (Platform.HALOOGLASI, False), (Platform.HALOOGLASI, True),
    (Platform.CITYEXPERT, True),
])
async def test_explicit_source_removal_preserves_last_content_and_success(db, platform, inactive):
    from datetime import UTC, datetime

    from src.scrapers.base import SourceListingInactiveError, SourceListingRemovedError
    previous = datetime(2026, 8, 1, tzinfo=UTC)
    row = await seed(platform=platform, price_eur=650, area_m2=45,
                     photo_urls=['https://img.test/old.jpg'], photo_phashes=['ff00ff00ff00ff00'],
                     details_succeeded_at=previous)
    class Removed(scraper_for(detail(row))):
        async def fetch_detail(self, url):
            raise SourceListingInactiveError() if inactive else SourceListingRemovedError()
    stats = await orch.refetch_listing_details(
        [row], sleep_between=0, scrapers={row.platform: Removed},
    )
    stored = await read(row)
    assert not stored.is_active and stored.link_status == 'DEAD'
    assert stored.price_eur == 650 and stored.area_m2 == 45
    assert stored.photo_urls == ['https://img.test/old.jpg']
    assert stored.photo_phashes == ['ff00ff00ff00ff00']
    assert stored.details_succeeded_at.replace(tzinfo=UTC) == previous
    assert stats.deactivated == 1 and stats.errors == 1 and stats.verified == 0
