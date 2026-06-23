"""Orchestrate one scraping pass for a single platform.

Flow:
1. list_recent() → preview rows.
2. For each preview: skip if Listing(platform, external_id) already exists AND was scraped recently;
   otherwise fetch_detail().
3. Download photos to data/listings/<platform>/<external_id>/, compute pHash for each.
4. Upsert Listing row.

Idempotent: re-running won't duplicate listings; will only refresh last_seen_at on existing rows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

import httpx
from sqlalchemy import func, select, update

from src.config import get_settings
from src.db import Listing, get_session
from src.db.models import DailyMetrics, Platform
from src.matching.phash import download_and_hash
from src.scrapers.base import ListingDetail, Scraper

logger = logging.getLogger(__name__)


def _last_scrape_marker_path() -> Path:
    return get_settings().data_dir / "state" / "last_scrape.txt"


async def scrape_all_with_throttle(
    *,
    throttle_minutes: int = 15,
    max_pages: int = 2,
    max_new: int | None = 50,
) -> tuple[bool, list[ScrapeResult]]:
    """Refresh all platforms unless last scrape was less than `throttle_minutes` ago.

    Returns `(ran, results)`. `ran=False` means we were throttled.

    The marker lives at `data/state/last_scrape.txt` and uses file mtime as the timestamp —
    no DB migration needed, atomic on POSIX (one writer at a time within the same process).
    """
    marker = _last_scrape_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if marker.exists():
        age_sec = now - marker.stat().st_mtime
        if age_sec < throttle_minutes * 60:
            logger.info(
                "[scrape] throttled — last run %.1f min ago < %d min",
                age_sec / 60, throttle_minutes,
            )
            return False, []

    # Touch *before* running, so a parallel trigger doesn't race in.
    marker.write_text(str(int(now)))

    # Lazy import to avoid circular: src.scrapers.__init__ -> nekretnine/4zida/halooglasi -> base
    from src.scrapers import SCRAPERS

    results: list[ScrapeResult] = []
    for plat, scraper_cls in SCRAPERS.items():
        try:
            async with scraper_cls() as scraper:
                r = await scrape_platform(
                    scraper, max_pages=max_pages, max_new=max_new
                )
                results.append(r)
                logger.info(
                    "[scrape %s] previewed=%d new=%d refreshed=%d errors=%d",
                    plat.value, r.previewed, r.inserted, r.updated, r.errors,
                )
        except NotImplementedError as e:
            logger.info("[scrape %s] skipped: %s", plat.value, e)
        except Exception:
            logger.exception("[scrape %s] failed", plat.value)
    return True, results


@dataclass
class ScrapeResult:
    platform: Platform
    previewed: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    photos_downloaded: int = 0
    errors: int = 0
    skipped_pages: int = 0  # list pages we couldn't fetch and walked past (partial under-run signal)
    site_total: int | None = None  # listings the SITE says exist for the search (coverage denominator)


async def scrape_platform(
    scraper: Scraper,
    *,
    max_pages: int = 5,
    max_new: int | None = None,
    download_photos: bool = True,
    incremental: bool = True,
) -> ScrapeResult:
    """Run one scraping pass for a single platform.

    `incremental=True` (default) preloads existing external_ids for this platform and passes them
    to the scraper, so the scraper can stop paginating after it sees full pages of already-known
    items. First run is full; subsequent runs are fast (1-5 min instead of 6-7 h).
    """
    result = ScrapeResult(platform=scraper.platform)

    known_ids: set[str] = set()
    if incremental:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(Listing.external_id).where(Listing.platform == scraper.platform)
                )
            ).scalars().all()
            known_ids = set(rows)
        logger.info(
            "[%s] incremental mode — %d known external_ids in DB",
            scraper.platform.value, len(known_ids),
        )

    previews = await scraper.list_recent(max_pages=max_pages, known_ids=known_ids)
    result.previewed = len(previews)
    result.skipped_pages = getattr(scraper, "_skipped_pages", 0)
    result.site_total = getattr(scraper, "_site_total", None)
    logger.info(
        "[%s] list_recent → %d previews (skipped_pages=%d, site_total=%s)",
        scraper.platform.value, len(previews), result.skipped_pages, result.site_total,
    )

    new_count = 0
    for prev in previews:
        if max_new is not None and new_count >= max_new:
            break
        try:
            inserted, photos = await _process_one(
                scraper, prev.url, prev.external_id, download_photos=download_photos
            )
        except Exception:
            logger.exception("[%s] error processing %s", scraper.platform.value, prev.url)
            result.errors += 1
            continue

        result.fetched += 1
        result.photos_downloaded += photos
        if inserted is True:
            result.inserted += 1
            new_count += 1
        else:
            result.updated += 1
    return result


async def _process_one(
    scraper: Scraper, url: str, external_id: str, *, download_photos: bool
) -> tuple[bool | None, int]:
    """Return (inserted, photos_downloaded): inserted is True for a new Listing, False when an
    existing row was refreshed, None if skipped."""
    async with get_session() as session:
        existing = (
            await session.execute(
                select(Listing).where(
                    Listing.platform == scraper.platform,
                    Listing.external_id == external_id,
                )
            )
        ).scalar_one_or_none()

    if existing is not None:
        async with get_session() as session:
            stmt = select(Listing).where(Listing.id == existing.id)
            row = (await session.execute(stmt)).scalar_one()
            row.last_seen_at = datetime.now(timezone.utc)
            # Self-heal stale URLs: the platform can rewrite a listing's slug/URL while the
            # listing stays in the search results (matched by external_id). The preview URL is
            # the source of truth (now canonical numeric form) — adopt it so the stored link
            # never goes 404 on the site / in match cards.
            if url and row.url != url:
                row.url = url
            # Reactivate: a listing we just saw on the platform is live again. Without this, a
            # listing wrongly swept inactive (e.g. after a transient scrape outage) would stay
            # is_active=False forever even though it's back — and the matcher only considers
            # active listings.
            if not row.is_active:
                row.is_active = True
        return False, 0

    detail = await scraper.fetch_detail(url)

    if not detail.photo_urls and detail.price_eur is None and detail.title is None:
        # All-None shell — the detail fetch "succeeded" on a challenge/error page or a payload
        # the parser no longer understands. Inserting it would freeze the listing forever
        # (existing rows are never detail-refreshed): no photos → invisible to the matcher,
        # while its external_id blocks any future re-fetch. Fail so the next walk retries.
        raise RuntimeError(f"empty detail payload for {url} — not inserting a dead row")

    photo_paths: list[Path] = []
    photo_hashes: list[str] = []
    embeddings_blob: bytes | None = None
    if download_photos and detail.photo_urls:
        photo_paths, photo_hashes, embeddings_blob = await _download_photos(detail)

    async with get_session() as session:
        listing = Listing(
            platform=detail.platform,
            external_id=detail.external_id,
            url=detail.url,
            price_eur=detail.price_eur,
            area_m2=detail.area_m2,
            rooms=detail.rooms,
            district=detail.district,
            address_raw=detail.address_raw,
            lat=detail.lat,
            lon=detail.lon,
            title=detail.title,
            description=detail.description,
            photo_urls=detail.photo_urls,
            photo_phashes=photo_hashes,
            photo_embeddings=embeddings_blob,
            raw_payload=detail.raw_payload,
            # Details were just fetched — start the rolling-refresh clock now instead of
            # leaving NULL (NULL = "never fetched", which jumps the refresh queue).
            details_refreshed_at=datetime.now(timezone.utc),
        )
        session.add(listing)
    return True, len(photo_paths)


async def _download_photos(
    detail: ListingDetail,
) -> tuple[list[Path], list[str], bytes | None]:
    """Download every photo, compute pHash, batch CLIP embeddings. Returns (paths, phashes, embeddings_blob)."""
    settings = get_settings()
    settings.ensure_dirs()
    target_dir = settings.listings_photos_dir / detail.platform.value / detail.external_id
    target_dir.mkdir(parents=True, exist_ok=True)

    headers = _photo_request_headers(detail.url)
    # Parallel download — up to 4 concurrent fetches per listing. Major speed-up: a 10-photo
    # listing drops from ~5s sequential to ~1.5s. CDN rate limits remain respected because
    # different listings still run serially in the outer loop.
    sem = asyncio.Semaphore(4)
    results: list[tuple[int, Path, str] | None] = [None] * len(detail.photo_urls)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        headers=headers,
    ) as client:
        async def _one(idx: int, url: str) -> None:
            async with sem:
                ext = _guess_extension(url)
                target = target_dir / f"{idx:03d}{ext}"
                res = await download_and_hash(
                    url, target, client,
                    platform=detail.platform.value,
                    listing_external_id=detail.external_id,
                )
                if res is None:
                    return
                saved, phash = res
                results[idx] = (idx, saved, phash)

        await asyncio.gather(*(_one(i, u) for i, u in enumerate(detail.photo_urls)))

    paths: list[Path] = []
    hashes: list[str] = []
    for entry in results:
        if entry is None:
            continue
        _idx, saved, phash = entry
        paths.append(saved)
        hashes.append(phash)

    # Compute CLIP embeddings off the event loop (model inference is CPU-bound).
    embeddings_blob: bytes | None = None
    if paths:
        try:
            from src.matching.clip_embed import compute_embeddings_batch, pack_embeddings

            vecs = await asyncio.to_thread(
                compute_embeddings_batch, [p for p in paths], batch_size=4
            )
            embeddings_blob = pack_embeddings(vecs)
        except Exception:
            logger.exception("CLIP embedding failed for %s", detail.url)
    return paths, hashes, embeddings_blob


def _photo_request_headers(referer_url: str) -> dict[str, str]:
    from src.scrapers.base import DEFAULT_USER_AGENT
    from src.config import get_settings

    settings = get_settings()
    ua = settings.scraper_user_agent or DEFAULT_USER_AGENT
    return {
        "User-Agent": ua,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "sr,en;q=0.9,ru;q=0.8",
        "Referer": referer_url,
    }


def _guess_extension(url: str) -> str:
    lower = url.lower()
    for ext in (".webp", ".jpg", ".jpeg", ".png"):
        if ext in lower:
            return ext
    return ".jpg"


@dataclass
class StaleSweepResult:
    platform: Platform | None
    marked_inactive: int


async def mark_stale_inactive(
    *,
    inactive_after_days: int = 10,
    platform: Platform | None = None,
) -> StaleSweepResult:
    """Flip `is_active=False` on listings whose `last_seen_at` is older than the cutoff.

    Run right after a *full* (non-incremental) scrape pass: anything still served on the platform
    just had its `last_seen_at` refreshed, so what's left below the cutoff is genuinely gone from
    the platform (rented out / unpublished / paused).

    Default 10 days = 7-day cadence + 3-day grace, so a missed weekly run doesn't false-positive
    everything.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=inactive_after_days)
    async with get_session() as session:
        # Plain UPDATE — loading full ORM rows here pulls every photo_embeddings BLOB
        # (hundreds of MB after an outage backlog) just to flip a boolean.
        stmt = (
            update(Listing)
            .where(Listing.is_active.is_(True))
            .where(Listing.last_seen_at < cutoff)
            .values(is_active=False)
        )
        if platform is not None:
            stmt = stmt.where(Listing.platform == platform)
        res = await session.execute(stmt)
        marked = res.rowcount or 0
    logger.info(
        "[stale-sweep] marked %d listings inactive (cutoff=%s, platform=%s)",
        marked, cutoff.date(), platform.value if platform else "all",
    )
    return StaleSweepResult(platform=platform, marked_inactive=marked)


# ------------------------------------------------------------------ nightly maintenance steps


class PriceChange(NamedTuple):
    """W2-C2 cross-wave contract (consumed by the Wave-3 price-drop alert): one entry per
    updated listing whose price_eur changed. None ⇄ value transitions included; entries
    follow processing order."""

    listing_id: int
    old_price_eur: int | None
    new_price_eur: int | None


@dataclass
class DetailRefetchStats:
    """Outcome of a batch detail re-fetch (refetch-details CLI, heal & rolling steps)."""

    selected: int = 0
    updated: int = 0
    photos: int = 0
    errors: int = 0
    deactivated: int = 0  # rows flipped is_active=False on a 404/410 (listing removed)
    skipped: int = 0  # rows left unprocessed when the wall-clock budget (deadline) ran out
    price_changes: list[PriceChange] = field(default_factory=list)  # see PriceChange


@dataclass
class _RefetchOutcome:
    photos_added: int
    old_price: int | None
    new_price: int | None


async def refetch_listing_details(
    rows: Sequence[Any],
    *,
    download_photos: bool = True,
    touch_refreshed_at: bool = False,
    touch_on_error: bool = False,
    sleep_between: float = 0.3,
    scrapers: Mapping[Platform, type[Scraper]] | None = None,
    progress: Callable[[DetailRefetchStats, Any, Exception | None], None] | None = None,
    deadline: float | None = None,
) -> DetailRefetchStats:
    """Re-fetch detail pages for `rows` of (id, platform, url) and update the stored listings.

    Shared core of the `refetch-details` CLI command and the nightly heal / rolling-refresh
    steps. `touch_on_error=True` bumps `details_refreshed_at` even on failures so the rolling
    queue rotates past permanently-dead rows (404 etc.) instead of starving on them forever.
    `progress(stats, row, error)` is called after every row attempt (error=None on success).

    `deadline` is a `time.monotonic()` target: once it passes, ALL still-unprocessed rows
    (across every platform) are counted in `stats.skipped` and processing stops cleanly, so a
    night of slow Cloudflare-challenged refetches can't overrun a shared wall-clock budget.
    """
    if scrapers is None:
        from src.scrapers import SCRAPERS  # lazy: avoids circular import

        scrapers = SCRAPERS

    stats = DetailRefetchStats(selected=len(rows))
    by_platform: dict[Platform, list[Any]] = {}
    for row in rows:
        by_platform.setdefault(row.platform, []).append(row)

    budget_exhausted = False
    for plat, plat_rows in by_platform.items():
        if budget_exhausted:
            # Deadline already hit in an earlier platform — count this whole platform's rows.
            stats.skipped += len(plat_rows)
            continue
        scraper_cls = scrapers[plat]
        attempted = 0
        try:
            async with scraper_cls() as scraper:
                for row in plat_rows:
                    if deadline is not None and time.monotonic() > deadline:
                        # Stop ALL remaining work; un-attempted rows here + every later
                        # platform's rows are skipped (counted below / in the outer loop).
                        budget_exhausted = True
                        stats.skipped += len(plat_rows) - attempted
                        break
                    attempted += 1
                    try:
                        outcome = await _refetch_one_listing(
                            scraper,
                            row.id,
                            row.url,
                            download_photos=download_photos,
                            touch_refreshed_at=touch_refreshed_at,
                        )
                        if outcome is None:
                            continue  # row deleted meanwhile
                        stats.updated += 1
                        stats.photos += outcome.photos_added
                        if outcome.old_price != outcome.new_price:
                            stats.price_changes.append(
                                PriceChange(row.id, outcome.old_price, outcome.new_price)
                            )
                        if progress is not None:
                            with suppress(Exception):
                                progress(stats, row, None)
                        if sleep_between:
                            await asyncio.sleep(sleep_between)
                    except Exception as e:
                        stats.errors += 1
                        logger.warning(
                            "[refetch %s] #%s (%s) failed: %s: %s",
                            plat.value, row.id, row.url, type(e).__name__, e,
                        )
                        status = getattr(getattr(e, "response", None), "status_code", None)
                        if status in (404, 410):
                            # Listing removed from the platform — deactivate so we never serve a
                            # broken link (instead of merely bumping the refresh timer).
                            async with get_session() as s:
                                await s.execute(
                                    update(Listing)
                                    .where(Listing.id == row.id)
                                    .values(is_active=False)
                                )
                            stats.deactivated += 1
                            logger.info("[refetch %s] #%s → %s, deactivated", plat.value, row.id, status)
                        elif touch_on_error:
                            async with get_session() as s:
                                await s.execute(
                                    update(Listing)
                                    .where(Listing.id == row.id)
                                    .values(details_refreshed_at=datetime.now(timezone.utc))
                                )
                        if progress is not None:
                            with suppress(Exception):
                                progress(stats, row, e)
        except Exception:
            # Scraper construction/teardown blew up — rows never attempted count as errors.
            stats.errors += max(len(plat_rows) - attempted, 0)
            logger.exception("[refetch %s] platform failed", plat.value)
    return stats


async def _refetch_one_listing(
    scraper: Scraper,
    listing_id: int,
    url: str,
    *,
    download_photos: bool,
    touch_refreshed_at: bool,
) -> _RefetchOutcome | None:
    # Hard cap one fetch: a pathological page (goto + content-poll × retries + browser recycle)
    # can otherwise run minutes. TimeoutError raises here and is handled by the caller's
    # `except Exception` (asyncio.TimeoutError subclasses Exception) like any other fetch failure.
    detail = await asyncio.wait_for(
        scraper.fetch_detail(url),
        timeout=get_settings().refetch_per_fetch_timeout_sec,
    )
    if not detail.photo_urls and detail.price_eur is None and detail.title is None:
        raise RuntimeError("still an empty payload")

    # Read current state and DOWNLOAD photos before touching the row: committing the new
    # photo_urls first would make a failed download undetectable forever (urls match,
    # phashes non-empty → no filter ever selects the row again).
    async with get_session() as s:
        current = await s.get(Listing, listing_id)
        if current is None:
            return None
        old_urls = list(current.photo_urls or [])
        old_phash_count = len(current.photo_phashes or [])
        old_price = current.price_eur

    need_photos = bool(
        download_photos
        and detail.photo_urls
        and (
            list(detail.photo_urls) != old_urls
            or old_phash_count < len(detail.photo_urls)
        )
    )
    hashes: list[str] = []
    emb: bytes | None = None
    if need_photos:
        _paths, hashes, emb = await _download_photos(detail)
        if not hashes:
            raise RuntimeError("photo download produced no hashes — row left untouched")

    async with get_session() as s:
        listing = await s.get(Listing, listing_id)
        if listing is None:
            return None
        listing.price_eur = detail.price_eur
        listing.area_m2 = detail.area_m2
        listing.rooms = detail.rooms
        listing.district = detail.district
        listing.address_raw = detail.address_raw
        listing.lat = detail.lat
        listing.lon = detail.lon
        listing.title = detail.title
        listing.description = detail.description
        listing.raw_payload = detail.raw_payload
        if need_photos:
            listing.photo_urls = detail.photo_urls
            listing.photo_phashes = hashes
            listing.photo_embeddings = emb
        if touch_refreshed_at:
            listing.details_refreshed_at = datetime.now(timezone.utc)
    return _RefetchOutcome(
        photos_added=len(hashes), old_price=old_price, new_price=detail.price_eur
    )


@dataclass
class HealResult:
    """W2-C1 outcome: «heal: исправлено {fixed} / осталось {remaining}»."""

    selected: int
    fixed: int
    remaining: int
    errors: int
    skipped: int = 0  # rows left unprocessed when the shared nightly budget ran out


# "Broken" = active listing the matcher can't see: no photo hashes at all.
_BROKEN_LISTING = (Listing.is_active.is_(True), Listing.photo_phashes == [])


async def heal_broken_listings(
    *,
    limit: int = 500,
    scrapers: Mapping[Platform, type[Scraper]] | None = None,
    deadline: float | None = None,
) -> HealResult:
    """W2-C1: re-fetch details/photos for up to `limit` broken active listings.

    `fixed` counts selected rows that are no longer broken afterwards; `remaining` is the
    total broken count left in the DB (selected-but-unfixed + never-selected overflow).
    """
    async with get_session() as s:
        # Rotate, don't starve: touch_on_error stamps failed rows' details_refreshed_at, and
        # selecting oldest-stamp-first pushes permanently-dead rows (404s, photo-less listings)
        # behind never-tried ones instead of re-fetching the same dead head every night.
        rows = (
            await s.execute(
                select(Listing.id, Listing.platform, Listing.url)
                .where(*_BROKEN_LISTING)
                .order_by(Listing.details_refreshed_at.asc().nulls_first(), Listing.id)
                .limit(limit)
            )
        ).all()

    stats = await refetch_listing_details(
        rows,
        download_photos=True,
        touch_refreshed_at=True,
        touch_on_error=True,
        scrapers=scrapers,
        deadline=deadline,
    )

    selected_ids = [r.id for r in rows]
    async with get_session() as s:
        still_broken_selected = 0
        if selected_ids:
            still_broken_selected = (
                await s.execute(
                    select(func.count(Listing.id)).where(
                        *_BROKEN_LISTING, Listing.id.in_(selected_ids)
                    )
                )
            ).scalar_one()
        remaining = (
            await s.execute(select(func.count(Listing.id)).where(*_BROKEN_LISTING))
        ).scalar_one()
    logger.info(
        "[heal] selected=%d fixed=%d remaining=%d errors=%d skipped=%d",
        len(rows), len(rows) - still_broken_selected, remaining, stats.errors, stats.skipped,
    )
    return HealResult(
        selected=len(rows),
        fixed=len(rows) - still_broken_selected,
        remaining=remaining,
        errors=stats.errors,
        skipped=stats.skipped,
    )


@dataclass
class RollingRefreshResult:
    """W2-C2 outcome — `price_changes` is the return contract for the Wave-3 price-drop
    alert, see PriceChange."""

    selected: int
    refreshed: int
    errors: int
    price_changes: list[PriceChange]
    skipped: int = 0  # rows left unprocessed when the shared nightly budget ran out


async def rolling_detail_refresh(
    *,
    limit: int = 200,
    scrapers: Mapping[Platform, type[Scraper]] | None = None,
    deadline: float | None = None,
) -> RollingRefreshResult:
    """W2-C2: re-fetch details for the `limit` ACTIVE listings with the oldest
    `details_refreshed_at` (NULL = never → first), split evenly across platforms.

    Per-platform quota is limit//n (+1 for the first limit%n platforms); a platform with
    fewer rows than its quota does NOT donate the leftover to others — keeps selection simple
    and the nightly budget bounded.
    """
    if scrapers is None:
        from src.scrapers import SCRAPERS  # lazy: avoids circular import

        scrapers = SCRAPERS

    platforms = list(scrapers.keys())
    base, rem = divmod(max(limit, 0), len(platforms))
    rows: list[Any] = []
    async with get_session() as s:
        for i, plat in enumerate(platforms):
            quota = base + (1 if i < rem else 0)
            if quota <= 0:
                continue
            plat_rows = (
                await s.execute(
                    select(Listing.id, Listing.platform, Listing.url)
                    .where(Listing.platform == plat, Listing.is_active.is_(True))
                    .order_by(Listing.details_refreshed_at.asc().nulls_first(), Listing.id)
                    .limit(quota)
                )
            ).all()
            rows.extend(plat_rows)

    stats = await refetch_listing_details(
        rows,
        download_photos=True,  # only re-downloads when the photo set actually changed
        touch_refreshed_at=True,
        touch_on_error=True,
        scrapers=scrapers,
        deadline=deadline,
    )
    logger.info(
        "[rolling-refresh] selected=%d refreshed=%d price_changes=%d errors=%d skipped=%d",
        len(rows), stats.updated, len(stats.price_changes), stats.errors, stats.skipped,
    )
    return RollingRefreshResult(
        selected=len(rows),
        refreshed=stats.updated,
        errors=stats.errors,
        price_changes=stats.price_changes,
        skipped=stats.skipped,
    )


async def prune_inactive_embeddings(*, older_than_days: int = 30) -> int:
    """W2-C3: NULL out photo_embeddings of listings inactive for > `older_than_days`.

    Frees pages INSIDE otz.db only — the file shrinks solely after a manual VACUUM
    (backup + stopped monitor required; see docs/PHASE5_DEPLOY.md §9). Never automated.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    async with get_session() as s:
        res = await s.execute(
            update(Listing)
            .where(
                Listing.is_active.is_(False),
                Listing.last_seen_at < cutoff,
                Listing.photo_embeddings.is_not(None),
            )
            .values(photo_embeddings=None)
        )
        pruned = res.rowcount or 0
    logger.info("[prune-embeddings] cleared %d rows (inactive > %d days)", pruned, older_than_days)
    return pruned


@dataclass
class MetricsSnapshotResult:
    """W2-C5 outcome. `drops`: (platform_value, prev_active, cur_active, prev_snapshot_date)
    per platform whose active_count fell by more than `drop_threshold` vs its most recent
    earlier snapshot (which can be days old after an outage — hence the date)."""

    snapshot_date: str
    rows_written: int
    drops: list[tuple[str, int, int, str]]
    drop_threshold: float


async def snapshot_daily_metrics(
    *,
    drop_threshold: float = 0.2,
    snapshot_date: str | None = None,
) -> MetricsSnapshotResult:
    """W2-C5: upsert today's per-platform (active_count, no_photo_count) into daily_metrics
    and detect >threshold day-over-day active-count drops ("scraper silently died, sweep
    deactivated everything" guard)."""
    today = snapshot_date or datetime.now(timezone.utc).date().isoformat()
    drops: list[tuple[str, int, int, str]] = []
    written = 0
    async with get_session() as s:
        for plat in Platform:
            active = (
                await s.execute(
                    select(func.count(Listing.id)).where(
                        Listing.platform == plat, Listing.is_active.is_(True)
                    )
                )
            ).scalar_one()
            no_photo = (
                await s.execute(
                    select(func.count(Listing.id)).where(
                        Listing.platform == plat,
                        Listing.is_active.is_(True),
                        Listing.photo_phashes == [],
                    )
                )
            ).scalar_one()

            prev = (
                await s.execute(
                    select(DailyMetrics)
                    .where(
                        DailyMetrics.platform == plat.value,
                        DailyMetrics.snapshot_date < today,
                    )
                    .order_by(DailyMetrics.snapshot_date.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if (
                prev is not None
                and prev.active_count > 0
                and active < (1 - drop_threshold) * prev.active_count
            ):
                drops.append((plat.value, prev.active_count, active, prev.snapshot_date))

            existing = (
                await s.execute(
                    select(DailyMetrics).where(
                        DailyMetrics.platform == plat.value,
                        DailyMetrics.snapshot_date == today,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.active_count = active
                existing.no_photo_count = no_photo
            else:
                s.add(
                    DailyMetrics(
                        snapshot_date=today,
                        platform=plat.value,
                        active_count=active,
                        no_photo_count=no_photo,
                    )
                )
            written += 1
    logger.info(
        "[daily-metrics] snapshot %s: %d rows, %d drop alert(s)", today, written, len(drops)
    )
    return MetricsSnapshotResult(
        snapshot_date=today, rows_written=written, drops=drops, drop_threshold=drop_threshold
    )
