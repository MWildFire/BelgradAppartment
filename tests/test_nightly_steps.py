"""Unit tests for the nightly maintenance steps (W2-C1..C6).

In-memory SQLite + fake scrapers — no network, no real photo downloads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

import src.db.session as db_session
from src.db.models import DailyMetrics, InboundPost, Listing, Platform, SourceChannel
from src.scrapers import orchestrator
from src.scrapers.base import ListingDetail

# In-memory DB comes from the shared `db` fixture in tests/conftest.py.

OLD = datetime(2020, 1, 1, tzinfo=UTC)
RECENT = datetime(2026, 6, 1, tzinfo=UTC)


async def seed_listing(
    ext: str,
    *,
    platform: Platform = Platform.NEKRETNINE,
    active: bool = True,
    price: int | None = None,
    phashes: list[str] | None = None,
    urls: list[str] | None = None,
    embeddings: bytes | None = None,
    refreshed_at: datetime | None = None,
    last_seen_at: datetime | None = None,
    title: str | None = None,
) -> int:
    async with db_session.get_session() as s:
        listing = Listing(
            platform=platform,
            external_id=ext,
            url=f"https://example.com/{platform.value}/{ext}",
            price_eur=price,
            title=title,
            photo_urls=urls or [],
            photo_phashes=phashes or [],
            photo_embeddings=embeddings,
            is_active=active,
            details_refreshed_at=refreshed_at,
            last_seen_at=last_seen_at or datetime.now(UTC),
        )
        s.add(listing)
        await s.flush()
        return listing.id


async def get_listing(listing_id: int) -> Listing:
    async with db_session.get_session() as s:
        row = await s.get(Listing, listing_id)
        assert row is not None
        return row


def make_fake_scraper(platform: Platform, details: dict[str, ListingDetail], fail: set[str] | None = None):
    """Class-factory: each test gets an isolated fake with call recording."""
    fetched: list[str] = []
    fail = fail or set()

    class _Fake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def fetch_detail(self, url: str) -> ListingDetail:
            fetched.append(url)
            if url in fail:
                raise RuntimeError("fetch failed")
            return details[url]

    _Fake.fetched = fetched  # type: ignore[attr-defined]
    return _Fake


def detail_for(listing_url: str, platform: Platform, **kw) -> ListingDetail:
    ext = listing_url.rsplit("/", 1)[-1]
    kw.setdefault("area_m2", 50)
    kw.setdefault("photo_urls", ["https://example.test/image.jpg"])
    return ListingDetail(platform=platform, external_id=ext, url=listing_url, **kw)


async def fake_download_photos(detail: ListingDetail, *, target_dir: Path | None = None):
    folder = target_dir or (
        orchestrator.get_settings().listings_photos_dir / detail.platform.value / detail.external_id
    )
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, url in enumerate(detail.photo_urls):
        path = folder / f"{index:03d}{orchestrator._guess_extension(url)}"
        Image.new("RGB", (8, 8), "blue").save(path)
        paths.append(path)
    return paths, ["ff00ff00ff00ff00"] * len(paths), None


@pytest.fixture(autouse=True)
def isolated_complete_media(tmp_path, monkeypatch):
    settings = orchestrator.get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "listings_photos_dir", tmp_path / "listings")
    monkeypatch.setattr(orchestrator, "_download_photos", fake_download_photos)


# ------------------------------------------------------------------ W2-C1 heal


async def test_heal_fixes_selected_and_counts_remaining(db, monkeypatch):
    monkeypatch.setattr(orchestrator, "_download_photos", fake_download_photos)
    b1 = await seed_listing("b1")
    b2 = await seed_listing("b2")
    b3 = await seed_listing("b3")  # beyond limit → stays broken
    await seed_listing("ok", phashes=["aa"])  # healthy, never selected

    urls = {i: f"https://example.com/NEKRETNINE/b{n}" for n, i in ((1, b1), (2, b2), (3, b3))}
    details = {
        u: detail_for(u, Platform.NEKRETNINE, price_eur=500, photo_urls=["http://img/1.jpg"])
        for u in urls.values()
    }
    fake = make_fake_scraper(Platform.NEKRETNINE, details)

    res = await orchestrator.heal_broken_listings(
        limit=2, scrapers={Platform.NEKRETNINE: fake}
    )

    assert res.selected == 2
    assert res.fixed == 2
    assert res.remaining == 1  # b3 untouched
    assert res.errors == 0
    assert fake.fetched == [urls[b1], urls[b2]]  # all-NULL refreshed_at → id order, capped by limit
    healed = await get_listing(b1)
    assert healed.photo_phashes == ["ff00ff00ff00ff00"]
    assert healed.price_eur == 500
    assert healed.details_refreshed_at is not None  # heal also stamps the rolling clock


async def test_heal_counts_failures_honestly(db, monkeypatch):
    monkeypatch.setattr(orchestrator, "_download_photos", fake_download_photos)
    await seed_listing("b1")
    b2 = await seed_listing("b2")
    u1 = "https://example.com/NEKRETNINE/b1"
    u2 = "https://example.com/NEKRETNINE/b2"
    details = {
        u1: detail_for(u1, Platform.NEKRETNINE, price_eur=400, photo_urls=["http://img/1.jpg"]),
    }
    fake = make_fake_scraper(Platform.NEKRETNINE, details, fail={u2})

    res = await orchestrator.heal_broken_listings(
        limit=10, scrapers={Platform.NEKRETNINE: fake}
    )

    assert res.selected == 2
    assert res.fixed == 1
    assert res.remaining == 1  # b2 still broken
    assert res.errors == 1
    assert (await get_listing(b2)).photo_phashes == []


async def test_heal_rotates_failed_rows_to_back_of_queue(db, monkeypatch):
    """Anti-starvation: a heal-failed row gets details_refreshed_at stamped, so the next
    night's nulls-first selection picks the NEXT broken batch instead of the same dead head."""
    monkeypatch.setattr(orchestrator, "_download_photos", fake_download_photos)
    b1 = await seed_listing("b1")
    b2 = await seed_listing("b2")
    u1 = "https://example.com/NEKRETNINE/b1"
    u2 = "https://example.com/NEKRETNINE/b2"

    # Night 1: limit=1 → b1 (all NULL, id order); its detail fetch fails (e.g. 404).
    fake1 = make_fake_scraper(Platform.NEKRETNINE, {}, fail={u1})
    res1 = await orchestrator.heal_broken_listings(limit=1, scrapers={Platform.NEKRETNINE: fake1})
    assert fake1.fetched == [u1]
    assert (res1.fixed, res1.errors) == (0, 1)
    assert (await get_listing(b1)).details_refreshed_at is not None  # error-stamped

    # Night 2: same limit selects b2 (NULL stamp) — not the dead b1 again.
    fake2 = make_fake_scraper(
        Platform.NEKRETNINE,
        {u2: detail_for(u2, Platform.NEKRETNINE, price_eur=300, photo_urls=["http://img/2.jpg"])},
    )
    res2 = await orchestrator.heal_broken_listings(limit=1, scrapers={Platform.NEKRETNINE: fake2})
    assert fake2.fetched == [u2]
    assert res2.fixed == 1
    assert (await get_listing(b2)).photo_phashes == ["ff00ff00ff00ff00"]


# ------------------------------------------------------------------ W2-C2 rolling refresh


async def test_rolling_selects_nulls_first_and_splits_evenly(db):
    # NEKRETNINE: never-refreshed, old, recent
    n_null = await seed_listing("n-null", price=100, title="t")
    n_old = await seed_listing("n-old", price=100, refreshed_at=OLD, title="t")
    n_recent = await seed_listing("n-recent", price=100, refreshed_at=RECENT, title="t")
    # CETIRI_ZIDA: same shape
    c_null = await seed_listing("c-null", platform=Platform.CETIRI_ZIDA, price=100, title="t")
    c_old = await seed_listing("c-old", platform=Platform.CETIRI_ZIDA, price=100, refreshed_at=OLD, title="t")
    await seed_listing("c-recent", platform=Platform.CETIRI_ZIDA, price=100, refreshed_at=RECENT, title="t")
    # inactive rows are never selected
    await seed_listing("n-inactive", active=False, price=100, title="t")

    def urls_for(plat: Platform, *exts: str) -> list[str]:
        return [f"https://example.com/{plat.value}/{e}" for e in exts]

    n_details = {
        u: detail_for(u, Platform.NEKRETNINE, price_eur=100, title="t")
        for u in urls_for(Platform.NEKRETNINE, "n-null", "n-old", "n-recent")
    }
    c_details = {
        u: detail_for(u, Platform.CETIRI_ZIDA, price_eur=100, title="t")
        for u in urls_for(Platform.CETIRI_ZIDA, "c-null", "c-old", "c-recent")
    }
    fake_n = make_fake_scraper(Platform.NEKRETNINE, n_details)
    fake_c = make_fake_scraper(Platform.CETIRI_ZIDA, c_details)

    res = await orchestrator.rolling_detail_refresh(
        limit=4,
        scrapers={Platform.NEKRETNINE: fake_n, Platform.CETIRI_ZIDA: fake_c},
    )

    # Even split 2+2; NULL details_refreshed_at first, then oldest.
    assert fake_n.fetched == urls_for(Platform.NEKRETNINE, "n-null", "n-old")
    assert fake_c.fetched == urls_for(Platform.CETIRI_ZIDA, "c-null", "c-old")
    assert res.selected == 4
    assert res.refreshed == 4
    assert res.errors == 0
    for lid in (n_null, n_old, c_null, c_old):
        row = await get_listing(lid)
        assert row.details_refreshed_at is not None
        assert row.details_refreshed_at.replace(tzinfo=UTC) > RECENT
    # Untouched rows keep their timestamps.
    assert (await get_listing(n_recent)).details_refreshed_at.replace(tzinfo=UTC) == RECENT


async def test_rolling_returns_price_change_contract(db):
    l1 = await seed_listing("p1", price=500, title="t")
    await seed_listing("p2", price=300, title="t")  # unchanged price → not in contract
    l3 = await seed_listing("p3", price=None, title="t")
    base = f"https://example.com/{Platform.NEKRETNINE.value}"
    details = {
        f"{base}/p1": detail_for(f"{base}/p1", Platform.NEKRETNINE, price_eur=450, title="t"),
        f"{base}/p2": detail_for(f"{base}/p2", Platform.NEKRETNINE, price_eur=300, title="t"),
        f"{base}/p3": detail_for(f"{base}/p3", Platform.NEKRETNINE, price_eur=250, title="t"),
    }
    fake = make_fake_scraper(Platform.NEKRETNINE, details)

    res = await orchestrator.rolling_detail_refresh(
        limit=3, scrapers={Platform.NEKRETNINE: fake}
    )

    # Contract: PriceChange(listing_id, old, new), only changed rows, None ⇄ value included.
    assert res.price_changes == [(l1, 500, 450), (l3, None, 250)]
    assert res.refreshed == 3
    assert (await get_listing(l1)).price_eur == 450


async def test_rolling_bumps_timestamp_on_error_to_rotate_queue(db):
    dead = await seed_listing("dead", price=100, title="t")
    url = f"https://example.com/{Platform.NEKRETNINE.value}/dead"
    fake = make_fake_scraper(Platform.NEKRETNINE, {}, fail={url})

    res = await orchestrator.rolling_detail_refresh(
        limit=1, scrapers={Platform.NEKRETNINE: fake}
    )

    assert res.errors == 1
    assert res.refreshed == 0
    # Without the bump this dead row would head the queue every night forever.
    assert (await get_listing(dead)).details_refreshed_at is not None


# ------------------------------------------------------------------ H1 refetch budget / cap


async def test_refetch_deadline_skips_all_unprocessed_rows(db, monkeypatch):
    """An already-passed deadline → ~0 rows processed, the rest counted as skipped (across
    every platform, not just the first one in the loop)."""
    monkeypatch.setattr(orchestrator, "_download_photos", fake_download_photos)
    n1 = await seed_listing("n1", price=100, title="t")
    n2 = await seed_listing("n2", price=100, title="t")
    c1 = await seed_listing("c1", platform=Platform.CETIRI_ZIDA, price=100, title="t")

    def url(plat, ext):
        return f"https://example.com/{plat.value}/{ext}"

    rows = []
    async with db_session.get_session() as s:
        for lid in (n1, n2, c1):
            row = await s.get(Listing, lid)
            rows.append(type("R", (), {"id": row.id, "platform": row.platform, "url": row.url})())

    n_details = {url(Platform.NEKRETNINE, e): detail_for(url(Platform.NEKRETNINE, e), Platform.NEKRETNINE, price_eur=100, title="t") for e in ("n1", "n2")}
    c_details = {url(Platform.CETIRI_ZIDA, "c1"): detail_for(url(Platform.CETIRI_ZIDA, "c1"), Platform.CETIRI_ZIDA, price_eur=100, title="t")}
    fake_n = make_fake_scraper(Platform.NEKRETNINE, n_details)
    fake_c = make_fake_scraper(Platform.CETIRI_ZIDA, c_details)

    import time as _time

    stats = await orchestrator.refetch_listing_details(
        rows,
        scrapers={Platform.NEKRETNINE: fake_n, Platform.CETIRI_ZIDA: fake_c},
        deadline=_time.monotonic() - 1,  # already in the past
        sleep_between=0,
    )

    assert stats.updated == 0
    assert stats.skipped == 3  # every row across both platforms
    assert fake_n.fetched == []
    assert fake_c.fetched == []


async def test_refetch_no_deadline_processes_all_rows(db, monkeypatch):
    """deadline=None → unchanged behavior: no early break, nothing skipped."""
    monkeypatch.setattr(orchestrator, "_download_photos", fake_download_photos)
    lid = await seed_listing("k1", price=100, title="t")
    async with db_session.get_session() as s:
        row = await s.get(Listing, lid)
        rows = [type("R", (), {"id": row.id, "platform": row.platform, "url": row.url})()]
    u = f"https://example.com/{Platform.NEKRETNINE.value}/k1"
    fake = make_fake_scraper(Platform.NEKRETNINE, {u: detail_for(u, Platform.NEKRETNINE, price_eur=100, title="t")})

    stats = await orchestrator.refetch_listing_details(
        rows, scrapers={Platform.NEKRETNINE: fake}, deadline=None, sleep_between=0
    )

    assert stats.updated == 1
    assert stats.skipped == 0
    assert fake.fetched == [u]


async def test_refetch_per_fetch_cap_counts_slow_fetch_as_error(db, monkeypatch):
    """A fetch_detail that sleeps past the per-fetch cap is counted as an error (not a hang),
    and touch_on_error rotation still stamps details_refreshed_at."""
    import asyncio as _asyncio

    monkeypatch.setattr(orchestrator, "_download_photos", fake_download_photos)
    # Tiny cap so the test is fast; the slow fetch sleeps well past it.
    settings = orchestrator.get_settings()
    monkeypatch.setattr(settings, "refetch_per_fetch_timeout_sec", 0.05)

    lid = await seed_listing("slow", price=100, title="t")
    async with db_session.get_session() as s:
        row = await s.get(Listing, lid)
        rows = [type("R", (), {"id": row.id, "platform": row.platform, "url": row.url})()]

    class _SlowFake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def fetch_detail(self, url):
            await _asyncio.sleep(5)  # never completes within the 0.05s cap
            raise AssertionError("should have been cancelled by the cap")

    stats = await orchestrator.refetch_listing_details(
        rows, scrapers={Platform.NEKRETNINE: _SlowFake}, touch_on_error=True, sleep_between=0
    )

    assert stats.errors == 1
    assert stats.updated == 0
    # touch_on_error rotation happened despite the timeout.
    assert (await get_listing(lid)).details_refreshed_at is not None


# ------------------------------------------------------------------ W2-C3 prune


async def test_prune_clears_only_long_inactive_embeddings(db):
    old_seen = datetime.now(UTC) - timedelta(days=40)
    fresh_seen = datetime.now(UTC) - timedelta(days=5)
    target = await seed_listing("gone", active=False, embeddings=b"\x01\x02", last_seen_at=old_seen)
    recent_inactive = await seed_listing("paused", active=False, embeddings=b"\x03", last_seen_at=fresh_seen)
    active_old = await seed_listing("live", active=True, embeddings=b"\x04", last_seen_at=old_seen)
    already_null = await seed_listing("empty", active=False, embeddings=None, last_seen_at=old_seen)

    pruned = await orchestrator.prune_inactive_embeddings(older_than_days=30)

    assert pruned == 1
    assert (await get_listing(target)).photo_embeddings is None
    assert (await get_listing(recent_inactive)).photo_embeddings == b"\x03"
    assert (await get_listing(active_old)).photo_embeddings == b"\x04"
    assert (await get_listing(already_null)).photo_embeddings is None


# ------------------------------------------------------------------ W2-C5 metrics


async def test_metrics_snapshot_upsert_and_drop_detection(db):
    for i in range(4):
        await seed_listing(f"n{i}", phashes=[] if i == 0 else ["aa"])
    await seed_listing("n-off", active=False)
    for i in range(4):
        await seed_listing(f"c{i}", platform=Platform.CETIRI_ZIDA, phashes=["aa"])

    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    async with db_session.get_session() as s:
        # NEKRETNINE collapsed 10 → 4 (>20% drop); CETIRI_ZIDA steady.
        s.add(DailyMetrics(snapshot_date=yesterday, platform="NEKRETNINE", active_count=10, no_photo_count=0))
        s.add(DailyMetrics(snapshot_date=yesterday, platform="CETIRI_ZIDA", active_count=4, no_photo_count=0))

    res = await orchestrator.snapshot_daily_metrics()

    assert res.rows_written == 4  # one per platform, incl. empty HALOOGLASI + CITYEXPERT
    assert res.drops == [("NEKRETNINE", 10, 4, yesterday)]
    assert res.drop_threshold == 0.2

    today = datetime.now(UTC).date().isoformat()
    async with db_session.get_session() as s:
        rows = (
            await s.execute(select(DailyMetrics).where(DailyMetrics.snapshot_date == today))
        ).scalars().all()
        by_plat = {r.platform: r for r in rows}
    assert len(rows) == 4
    assert by_plat["NEKRETNINE"].active_count == 4
    assert by_plat["NEKRETNINE"].no_photo_count == 1
    assert by_plat["CETIRI_ZIDA"].active_count == 4
    assert by_plat["HALOOGLASI"].active_count == 0
    assert by_plat["CITYEXPERT"].active_count == 0

    # Re-run same day → upsert, not duplicate rows.
    async with db_session.get_session() as s:
        row = (
            await s.execute(select(Listing).where(Listing.external_id == "n1"))
        ).scalar_one()
        row.is_active = False
    res2 = await orchestrator.snapshot_daily_metrics()
    async with db_session.get_session() as s:
        rows = (
            await s.execute(select(DailyMetrics).where(DailyMetrics.snapshot_date == today))
        ).scalars().all()
    assert len(rows) == 4
    assert {r.platform: r.active_count for r in rows}["NEKRETNINE"] == 3
    assert res2.rows_written == 4


async def test_metrics_no_drop_without_previous_snapshot(db):
    await seed_listing("n0")
    res = await orchestrator.snapshot_daily_metrics()
    assert res.drops == []


# ------------------------------------------------------------------ nightly step runner (cli)


def _canned_step_stubs(monkeypatch, *, heal_raises: bool = False, deadlines: list | None = None):
    """`deadlines` (if given) records the `deadline=` value each refetch step received, so a
    test can assert heal and rolling got the SAME shared deadline object."""
    calls: list[str] = []

    async def fake_heal(*, limit, deadline=None):
        calls.append(f"heal:{limit}")
        if deadlines is not None:
            deadlines.append(deadline)
        if heal_raises:
            raise RuntimeError("heal exploded")
        return orchestrator.HealResult(selected=5, fixed=5, remaining=2, errors=0)

    async def fake_rolling(*, limit, deadline=None):
        calls.append(f"rolling:{limit}")
        if deadlines is not None:
            deadlines.append(deadline)
        return orchestrator.RollingRefreshResult(
            selected=10, refreshed=10, errors=0,
            price_changes=[
                orchestrator.PriceChange(1, 500, 450),
                orchestrator.PriceChange(2, None, 300),
                orchestrator.PriceChange(3, 400, 410),
            ],
        )

    async def fake_metrics():
        calls.append("metrics")
        return orchestrator.MetricsSnapshotResult(
            snapshot_date="2026-06-12", rows_written=3,
            drops=[("NEKRETNINE", 100, 70, "2026-06-11")], drop_threshold=0.2,
        )

    async def fake_prune():
        calls.append("prune")
        return 7

    async def fake_verify_links(
        *, limit=None, deadline=None, platform=None, client=None, hides_allowed=True, guard_dir=None,
    ):
        # Intentionally records nothing: the call-order (`calls`) and shared-deadline
        # (`deadlines`) assertions track only the two refetch-budget steps. This stub exists so
        # A3's liveness sweep never hits the network/DB in these unit tests.
        return orchestrator.LinkVerifyStats(checked=0, deactivated=0, errors=0)

    async def fake_auto_publish_new_listings(*, since_hours=24, limit=None):
        # X1 backfill stub — records nothing (no network/DB) so the nightly summary tests stay
        # deterministic; returns 0 exposed cards.
        return 0

    async def fake_notify_subscriptions(*, since_hours=24, dry_run=False, database_url=None):
        # C2 stub — never touches the real DB so the nightly summary tests stay deterministic.
        import src.jobs.notify_subscriptions as _notify_subs_mod

        return _notify_subs_mod.NotifyStats()

    monkeypatch.setattr(orchestrator, "heal_broken_listings", fake_heal)
    monkeypatch.setattr(orchestrator, "rolling_detail_refresh", fake_rolling)
    monkeypatch.setattr(orchestrator, "snapshot_daily_metrics", fake_metrics)
    monkeypatch.setattr(orchestrator, "prune_inactive_embeddings", fake_prune)
    monkeypatch.setattr(orchestrator, "verify_active_links", fake_verify_links)
    monkeypatch.setattr(orchestrator, "auto_publish_new_listings", fake_auto_publish_new_listings)

    import src.jobs.notify_subscriptions as _notify_subs_mod

    monkeypatch.setattr(_notify_subs_mod, "notify_subscriptions", fake_notify_subscriptions)
    return calls


@pytest.mark.parametrize("errors", [0, 2])
async def test_all_inventory_step_overrides_zero_quota_and_reports_backoff(monkeypatch, errors):
    from src.cli import _run_nightly_steps
    from src.config import get_settings
    from src.scrapers import orchestrator

    settings = get_settings()
    calls = _canned_step_stubs(monkeypatch)
    monkeypatch.setattr(settings, "nightly_heal_limit", 500)
    monkeypatch.setattr(settings, "nightly_detail_refresh", 0)
    monkeypatch.setattr(settings, "nightly_detail_refresh_all", True)

    async def all_rows(*, limit, deadline, all_listings):
        assert limit == 0 and all_listings and deadline > 0
        return orchestrator.RollingRefreshResult(
            4001, 4001-errors, errors, [], inventory_count=4004, retry_deferred=3,
        )

    monkeypatch.setattr(orchestrator, "rolling_detail_refresh", all_rows)
    lines, *_ = await _run_nightly_steps()
    line = next(line for line in lines if "деталей обновлено" in line)
    assert "4001" in line and "4004" in line and "3" in line
    assert line.startswith("⚠️")
    assert not any(call.startswith("heal:") for call in calls)


async def test_run_nightly_steps_assembles_summary_lines(monkeypatch):
    from src.cli import _run_nightly_steps
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "nightly_heal_limit", 500)
    monkeypatch.setattr(settings, "nightly_detail_refresh", 200)
    monkeypatch.setattr(settings, "nightly_prune_embeddings", True)
    monkeypatch.setattr(settings, "nightly_metrics", True)
    calls = _canned_step_stubs(monkeypatch)

    lines, price_changes, alerts, _hidden, _alarm = await _run_nightly_steps()

    assert calls == ["heal:500", "rolling:200", "metrics", "prune"]
    assert "heal: исправлено 5 / осталось 2" in lines
    assert "деталей обновлено: 10, цен изменилось: 3" in lines
    assert "embeddings очищено (неактивные >30 дн): 7" in lines
    drop_lines = [ln for ln in lines if ln.startswith("🚨 NEKRETNINE: active 100 → 70")]
    assert len(drop_lines) == 1
    # Percentage derives from drop_threshold; the date names the actual comparison snapshot
    # (could be days back after an outage — not «за сутки»).
    assert "падение >20% против прошлого снапшота 2026-06-11" in drop_lines[0]
    assert alerts == drop_lines  # drop alert also goes out standalone
    # W2-C2 contract surfaces unchanged for the Wave-3 price-drop hook.
    assert price_changes == [(1, 500, 450), (2, None, 300), (3, 400, 410)]


async def test_run_nightly_steps_surfaces_site_sync_lines(monkeypatch):
    """X1/X2 wiring: verify-links line carries the auto-hidden count, and the auto-publish
    backfill step adds its own line when it exposes cards."""
    from src.cli import _run_nightly_steps
    from src.config import get_settings
    from src.scrapers import orchestrator

    settings = get_settings()
    monkeypatch.setattr(settings, "nightly_heal_limit", 0)
    monkeypatch.setattr(settings, "nightly_detail_refresh", 0)
    monkeypatch.setattr(settings, "nightly_prune_embeddings", False)
    monkeypatch.setattr(settings, "nightly_metrics", False)
    monkeypatch.setattr(settings, "nightly_verify_links", True)
    monkeypatch.setattr(settings, "nightly_auto_publish", True)
    _canned_step_stubs(monkeypatch)

    async def verify_with_hidden(
        *, limit=None, deadline=None, platform=None, client=None, hides_allowed=True, guard_dir=None,
    ):
        return orchestrator.LinkVerifyStats(
            checked=10, deactivated=2, errors=0, site_listings_hidden=2
        )

    async def backfill_exposes_three(*, since_hours=24, limit=None):
        return 3

    monkeypatch.setattr(orchestrator, "verify_active_links", verify_with_hidden)
    monkeypatch.setattr(orchestrator, "auto_publish_new_listings", backfill_exposes_three)

    lines, _price_changes, _alerts, _hidden, _alarm = await _run_nightly_steps()

    assert any("скрыто карточек с сайта: 2" in ln for ln in lines), lines
    assert any("авто-публикация на сайт: 3" in ln for ln in lines), lines


async def test_run_nightly_steps_failure_adds_alarm_line_not_crash(monkeypatch):
    from src.cli import _run_nightly_steps
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "nightly_heal_limit", 100)
    monkeypatch.setattr(settings, "nightly_detail_refresh", 100)
    monkeypatch.setattr(settings, "nightly_prune_embeddings", True)
    monkeypatch.setattr(settings, "nightly_metrics", True)
    calls = _canned_step_stubs(monkeypatch, heal_raises=True)

    lines, price_changes, alerts, _hidden, _alarm = await _run_nightly_steps()

    assert "🚨 heal-шаг упал — см. логи" in lines
    # The remaining steps still ran after the failure.
    assert calls == ["heal:100", "rolling:100", "metrics", "prune"]
    assert "деталей обновлено: 10, цен изменилось: 3" in lines
    assert price_changes  # rolling result still propagated
    assert alerts  # drop alert from metrics still emitted


async def test_run_nightly_steps_reserves_rolling_time_inside_total_budget(monkeypatch):
    """Heal must stop before rolling reaches the shared total deadline."""
    from src.cli import _run_nightly_steps
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "nightly_heal_limit", 500)
    monkeypatch.setattr(settings, "nightly_detail_refresh", 200)
    monkeypatch.setattr(settings, "nightly_prune_embeddings", False)
    monkeypatch.setattr(settings, "nightly_metrics", False)
    deadlines: list = []
    _canned_step_stubs(monkeypatch, deadlines=deadlines)

    await _run_nightly_steps()

    assert len(deadlines) == 2
    assert deadlines[0] is not None
    assert deadlines[0] < deadlines[1]  # later work retains a reserved slice
    assert deadlines[1] - deadlines[0] <= settings.nightly_refetch_budget_sec


async def test_run_nightly_steps_reports_budget_skipped(monkeypatch):
    """When the budget is exhausted, the summary lines for heal and rolling report skipped."""
    from src.cli import _run_nightly_steps
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "nightly_heal_limit", 500)
    monkeypatch.setattr(settings, "nightly_detail_refresh", 200)
    monkeypatch.setattr(settings, "nightly_prune_embeddings", False)
    monkeypatch.setattr(settings, "nightly_metrics", False)

    async def fake_heal(*, limit, deadline=None):
        return orchestrator.HealResult(selected=5, fixed=2, remaining=3, errors=0, skipped=3)

    async def fake_rolling(*, limit, deadline=None):
        return orchestrator.RollingRefreshResult(
            selected=10, refreshed=4, errors=0, price_changes=[], skipped=6
        )

    monkeypatch.setattr(orchestrator, "heal_broken_listings", fake_heal)
    monkeypatch.setattr(orchestrator, "rolling_detail_refresh", fake_rolling)

    lines, _price_changes, _alerts, _hidden, _alarm = await _run_nightly_steps()

    assert any(line.startswith("⚠️ heal: исправлено 2 / осталось 3, пропущено по бюджету: 3")
               for line in lines)
    assert any(line.startswith("⚠️ деталей обновлено: 4, цен изменилось: 0, пропущено по бюджету: 6")
               for line in lines)


async def test_run_nightly_steps_env_zero_skips_everything(monkeypatch):
    from src.cli import _run_nightly_steps
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "nightly_heal_limit", 0)
    monkeypatch.setattr(settings, "nightly_detail_refresh", 0)
    monkeypatch.setattr(settings, "nightly_prune_embeddings", False)
    monkeypatch.setattr(settings, "nightly_metrics", False)
    monkeypatch.setattr(settings, "nightly_verify_links", False)
    monkeypatch.setattr(settings, "nightly_auto_publish", False)
    monkeypatch.setattr(settings, "nightly_notify_subscriptions", False)  # C2
    calls = _canned_step_stubs(monkeypatch)

    lines, price_changes, alerts, _hidden, _alarm = await _run_nightly_steps()

    assert calls == []
    assert lines == []
    assert price_changes == []
    assert alerts == []


# ------------------------------------------------------------------ W2-C4 column-only selections


async def test_listing_ids_needing_embeddings_matches_old_semantics(db):
    from src.cli import _listing_ids_needing_embeddings

    sel1 = await seed_listing("e1", phashes=["aa"], embeddings=None)
    no_need = await seed_listing("e2", phashes=["aa"], embeddings=b"\x01")
    no_phash = await seed_listing("e3", phashes=[], embeddings=None)
    inactive = await seed_listing("e4", active=False, phashes=["aa"], embeddings=None)
    sel2 = await seed_listing("e5", phashes=["aa"], embeddings=b"")  # empty blob == missing

    assert await _listing_ids_needing_embeddings(force=False) == [sel1, sel2]
    # force → every ACTIVE id, like the old full-ORM filter did.
    assert await _listing_ids_needing_embeddings(force=True) == [sel1, no_need, no_phash, sel2]
    assert inactive not in await _listing_ids_needing_embeddings(force=True)


async def test_inbound_ids_needing_embeddings_matches_old_semantics(db):
    from src.cli import _inbound_ids_needing_embeddings

    async with db_session.get_session() as s:
        ch = SourceChannel(tg_id=-100500, username="src", is_active=True)
        s.add(ch)
        await s.flush()
        i1 = InboundPost(source_channel_id=ch.id, tg_message_id=1, photo_paths=["/p/1.jpg"])
        i2 = InboundPost(source_channel_id=ch.id, tg_message_id=2, photo_paths=["/p/2.jpg"], photo_embeddings=b"\x01")
        i3 = InboundPost(source_channel_id=ch.id, tg_message_id=3, photo_paths=[])
        s.add_all([i1, i2, i3])
        await s.flush()
        ids = (i1.id, i2.id, i3.id)

    assert await _inbound_ids_needing_embeddings(force=False) == [ids[0]]
    assert await _inbound_ids_needing_embeddings(force=True) == list(ids)


async def test_listing_rows_needing_phash_filters_and_limits(db):
    from src.cli import _listing_rows_needing_phash

    h1 = await seed_listing("h1", urls=["http://img/1.jpg"])
    await seed_listing("h2", phashes=["aa"], urls=["http://img/2.jpg"])  # already hashed
    await seed_listing("h3", urls=[])  # nothing to hash
    await seed_listing("h4", active=False, urls=["http://img/4.jpg"])  # inactive
    h5 = await seed_listing("h5", platform=Platform.CETIRI_ZIDA, urls=["http://img/5.jpg"])

    rows = await _listing_rows_needing_phash((), None)
    assert [r.id for r in rows] == [h1, h5]
    assert rows[0].photo_urls == ["http://img/1.jpg"]  # loop needs urls without ORM row

    only_4z = await _listing_rows_needing_phash(("cetiri_zida",), None)
    assert [r.id for r in only_4z] == [h5]

    capped = await _listing_rows_needing_phash((), 1)
    assert [r.id for r in capped] == [h1]


# ------------------------------------------------------------------ W2-C6 photo coverage


async def test_photo_coverage_by_platform(db):
    from src.cli import _photo_coverage_by_platform

    await seed_listing("v1", phashes=["aa"])
    await seed_listing("v2", phashes=[])
    await seed_listing("v3", phashes=[], active=False)  # inactive not counted
    await seed_listing("v4", platform=Platform.CETIRI_ZIDA, phashes=["aa"])

    coverage = dict((p, (n, a)) for p, n, a in await _photo_coverage_by_platform())

    assert coverage["NEKRETNINE"] == (1, 2)
    assert coverage["CETIRI_ZIDA"] == (0, 1)
    assert coverage["HALOOGLASI"] == (0, 0)


async def test_completed_refresh_with_source_timeout_is_reported_degraded(monkeypatch):
    from src.cli import _run_nightly_steps
    from src.config import get_settings
    _canned_step_stubs(monkeypatch)
    monkeypatch.setattr(get_settings(), 'nightly_detail_refresh', 2)

    async def deadline_after_commit(*, limit, deadline=None):
        return orchestrator.RollingRefreshResult(
            selected=2, refreshed=2, errors=0, price_changes=[],
            source_errors={'NEKRETNINE': 'TimeoutError'},
        )

    monkeypatch.setattr(orchestrator, 'rolling_detail_refresh', deadline_after_commit)
    result = await _run_nightly_steps(notify=False)
    assert any(line.startswith('🚨') and 'обновлено: 2' in line for line in result.summary_lines)


@pytest.mark.asyncio
async def test_run_nightly_steps_reports_withheld_hides_as_alarm(monkeypatch):
    """Q3.21: a verify run whose removal alarm withheld hides says so in the night's lines."""
    from src.cli import _run_nightly_steps
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "nightly_heal_limit", 0)
    monkeypatch.setattr(settings, "nightly_detail_refresh", 0)
    monkeypatch.setattr(settings, "nightly_prune_embeddings", False)
    monkeypatch.setattr(settings, "nightly_metrics", False)
    monkeypatch.setattr(settings, "nightly_verify_links", True)
    monkeypatch.setattr(settings, "nightly_auto_publish", False)
    _canned_step_stubs(monkeypatch)

    async def verify_alarmed(
        *, limit=None, deadline=None, platform=None, client=None, hides_allowed=True, guard_dir=None,
    ):
        return orchestrator.LinkVerifyStats(
            checked=10, deactivated=6, hides_withheld=6, removal_alarm=True,
        )

    monkeypatch.setattr(orchestrator, "verify_active_links", verify_alarmed)

    lines, _price_changes, _alerts, hidden, alarm = await _run_nightly_steps()

    assert hidden == 0 and alarm is True
    assert any("автоскрытие пропущено: 6" in line for line in lines)


@pytest.mark.asyncio
async def test_public_alarm_holds_the_nights_hides(monkeypatch, tmp_path):
    """Q3.21: an alarmed public detail run holds the guard, so the night's verify pass hides nothing."""
    from datetime import UTC, datetime

    from src.cli import _run_nightly_steps
    from src.scrapers import orchestrator, removal_guard

    _nightly_settings(monkeypatch, detail_refresh=0)
    removal_guard.write_step(tmp_path / "state", "public", status="alarm", now=datetime.now(UTC))
    _canned_step_stubs(monkeypatch)
    seen = {}

    async def verify_spy(*, limit=None, deadline=None, platform=None, client=None, hides_allowed=True, guard_dir=None):
        seen["hides_allowed"] = hides_allowed
        return orchestrator.LinkVerifyStats(checked=10, deactivated=0)

    monkeypatch.setattr(orchestrator, "verify_active_links", verify_spy)
    result = await _run_nightly_steps()

    assert seen["hides_allowed"] is False
    assert result.removal_alarm is True


@pytest.mark.asyncio
async def test_rolling_alarm_reaches_the_verify_hides(monkeypatch, tmp_path):
    """Q3.21: the rolling refresh's mass-removal alarm holds the guard for the verify pass."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from src.cli import _run_nightly_steps
    from src.scrapers import orchestrator, removal_guard

    _nightly_settings(monkeypatch, detail_refresh=200)
    removal_guard.write_step(tmp_path / "state", "public", status="clean", now=datetime.now(UTC))
    _canned_step_stubs(monkeypatch)

    async def rolling_alarmed(**_kwargs):
        return SimpleNamespace(
            price_changes=[], refreshed=4, errors=0, skipped=0, selected=10,
            inventory_count=10, deactivated=6, retry_deferred=0, source_errors={},
            already_removed=0,
        )

    seen = {}

    async def verify_spy(*, limit=None, deadline=None, platform=None, client=None, hides_allowed=True, guard_dir=None):
        seen["hides_allowed"] = hides_allowed
        return orchestrator.LinkVerifyStats(checked=10, deactivated=0)

    monkeypatch.setattr(orchestrator, "rolling_detail_refresh", rolling_alarmed)
    monkeypatch.setattr(orchestrator, "verify_active_links", verify_spy)
    result = await _run_nightly_steps()

    assert seen["hides_allowed"] is False
    assert result.removal_alarm is True


@pytest.mark.asyncio
async def test_clean_nights_release_the_hides(monkeypatch, tmp_path):
    """Every enabled step clean: the verify pass may hide and the night is not withheld."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from src.cli import _run_nightly_steps
    from src.scrapers import orchestrator, removal_guard

    _nightly_settings(monkeypatch, detail_refresh=200)
    yesterday = datetime.now(UTC) - timedelta(hours=20)
    removal_guard.write_step(tmp_path / "state", "public", status="clean", now=yesterday)
    removal_guard.write_step(tmp_path / "state", "verify", status="clean", now=yesterday)
    _canned_step_stubs(monkeypatch)

    async def rolling_quiet(**_kwargs):
        return SimpleNamespace(
            price_changes=[], refreshed=8, errors=0, skipped=0, selected=10,
            inventory_count=10, deactivated=0, retry_deferred=0, source_errors={},
            already_removed=0,
        )

    seen = {}

    async def verify_quiet(*, limit=None, deadline=None, platform=None, client=None, hides_allowed=True, guard_dir=None):
        seen["hides_allowed"] = hides_allowed
        removal_guard.write_step(tmp_path / "state", "verify", status="clean", now=datetime.now(UTC))
        return orchestrator.LinkVerifyStats(checked=10, deactivated=0)

    monkeypatch.setattr(orchestrator, "rolling_detail_refresh", rolling_quiet)
    monkeypatch.setattr(orchestrator, "verify_active_links", verify_quiet)
    result = await _run_nightly_steps()

    assert seen["hides_allowed"] is True
    assert result.removal_alarm is False


@pytest.mark.asyncio
async def test_verify_crash_holds_the_guard(monkeypatch, tmp_path):
    """A verify-links crash leaves its removals unjudged: the night hides nothing (Q3.21)."""
    from datetime import UTC, datetime

    from src.cli import _run_nightly_steps
    from src.scrapers import orchestrator, removal_guard

    _nightly_settings(monkeypatch, detail_refresh=0)
    removal_guard.write_step(tmp_path / "state", "public", status="clean", now=datetime.now(UTC))
    _canned_step_stubs(monkeypatch)

    async def verify_crashes(**_kwargs):
        raise RuntimeError("verify exploded")

    monkeypatch.setattr(orchestrator, "verify_active_links", verify_crashes)
    result = await _run_nightly_steps()

    assert result.removal_alarm is True
    assert any("verify-links шаг упал" in line for line in result.summary_lines)


@pytest.mark.asyncio
async def test_rolling_crash_withholds_the_verify_hides(monkeypatch, tmp_path):
    """A detail-refresh crash leaves the removal state unknown: the verify pass hides nothing."""
    from datetime import UTC, datetime

    from src.cli import _run_nightly_steps
    from src.scrapers import orchestrator, removal_guard

    _nightly_settings(monkeypatch, detail_refresh=200)
    removal_guard.write_step(tmp_path / "state", "public", status="clean", now=datetime.now(UTC))
    _canned_step_stubs(monkeypatch)
    seen = {}

    async def rolling_crashes(**_kwargs):
        raise RuntimeError("rolling exploded")

    async def verify_spy(*, limit=None, deadline=None, platform=None, client=None, hides_allowed=True, guard_dir=None):
        seen["hides_allowed"] = hides_allowed
        return orchestrator.LinkVerifyStats(checked=10, deactivated=0)

    monkeypatch.setattr(orchestrator, "rolling_detail_refresh", rolling_crashes)
    monkeypatch.setattr(orchestrator, "verify_active_links", verify_spy)
    result = await _run_nightly_steps()

    assert seen["hides_allowed"] is False
    assert result.removal_alarm is True


def _nightly_settings(monkeypatch, *, detail_refresh: int):
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "nightly_heal_limit", 0)
    monkeypatch.setattr(settings, "nightly_detail_refresh_all", False)
    monkeypatch.setattr(settings, "nightly_detail_refresh", detail_refresh)
    monkeypatch.setattr(settings, "nightly_prune_embeddings", False)
    monkeypatch.setattr(settings, "nightly_metrics", False)
    monkeypatch.setattr(settings, "nightly_verify_links", True)
    monkeypatch.setattr(settings, "nightly_auto_publish", False)
    return settings


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)


@pytest.mark.asyncio
async def test_previous_night_verify_alarm_closes_the_next_verify_pass(monkeypatch, tmp_path):
    """Q3.21: a verify alarm from last night still holds tonight's verify hide pass."""
    from datetime import UTC, datetime, timedelta

    from src.cli import _run_nightly_steps
    from src.scrapers import orchestrator, removal_guard

    _nightly_settings(monkeypatch, detail_refresh=0)
    state = tmp_path / "state"
    now = datetime.now(UTC)
    removal_guard.write_step(state, "public", status="clean", now=now)
    removal_guard.write_step(
        state, "verify", status="alarm", now=now - timedelta(hours=12),
        deactivated=6, attempted=10,
    )
    _canned_step_stubs(monkeypatch)
    seen = {}

    async def verify_spy(*, limit=None, deadline=None, platform=None, client=None,
                         hides_allowed=True, guard_dir=None):
        seen["hides_allowed"] = hides_allowed
        return orchestrator.LinkVerifyStats(checked=10, deactivated=0)

    monkeypatch.setattr(orchestrator, "verify_active_links", verify_spy)
    await _run_nightly_steps()

    assert seen["hides_allowed"] is False
