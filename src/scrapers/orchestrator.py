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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from src.config import get_settings
from src.db import Listing, get_session
from src.db.models import Platform
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
    from sqlalchemy import update

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
