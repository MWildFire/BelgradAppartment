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
import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from sqlalchemy import func, select, update

from src.config import get_settings
from src.db import Listing, get_session
from src.db.models import DailyMetrics, Match, MatchStatus, Platform, PublishedPost, SiteListing
from src.matching.phash import download_and_hash
from src.scrapers.base import ListingDetail, ListingPreview, Scraper
from src.scrapers.freshness import (
    CallbackResult,
    FairRefreshPlan,
    RefreshCandidate,
    RefreshEvidence,
    assess_detail,
    load_candidates,
    record_attempt,
    run_fair_refresh,
    select_fair,
)
from src.scrapers.link_probe import SourceProbeSession
from src.scrapers.run_safety import evaluate_absence, evaluate_source
from src.scrapers.snapshot_parity import mismatch_fields

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
        except Exception as exc:
            results.append(ScrapeResult.failed(plat, reason=type(exc).__name__))
            logger.exception("[scrape %s] failed", plat.value)
    return True, results


@dataclass
class ScrapeResult:
    platform: Platform
    run_id: str = field(default_factory=lambda: uuid4().hex)
    full_crawl: bool = False
    crawl_complete: bool = False
    stop_reason: str = "unverified"
    pages_attempted: int = 0
    pages_failed: int = 0
    denominator_trusted: bool = False
    quality_reasons: list[str] = field(default_factory=list)
    observed_ids: set[str] = field(default_factory=set, repr=False)
    dry_run: bool = False
    previewed: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    photos_downloaded: int = 0
    errors: int = 0
    skipped_pages: int = 0  # list pages we couldn't fetch and walked past (partial under-run signal)
    site_total: int | None = None  # listings the SITE says exist for the search (coverage denominator)
    # B1 — per-parser parse-success-rate. In-memory only (no DB migration this phase).
    # details_attempted: a detail page was actually fetched (re-seen rows DON'T count).
    # details_parsed_ok: that fetch produced a non-shell detail we inserted.
    # empty_shells: the all-None-shell guard fired (markup-change signal).
    details_attempted: int = 0
    details_parsed_ok: int = 0
    empty_shells: int = 0
    # Zero-regression snapshot/proxy telemetry. In-memory only; no schema or protected pipeline
    # changes are needed for the migration gate.
    snapshot_mode: str = "legacy"
    snapshots_seen: int = 0
    snapshots_complete: int = 0
    snapshots_incomplete: int = 0
    snapshot_fallbacks: int = 0
    snapshot_active_uses: int = 0
    snapshot_shadow_mismatches: int = 0
    proxy_metrics: dict[str, int] = field(default_factory=dict)

    @classmethod
    def failed(
        cls, platform: Platform, *, reason: str = "source_exception", full_crawl: bool = True
    ):
        return cls(platform=platform, full_crawl=full_crawl, stop_reason="source_exception",
                   errors=1, quality_reasons=[reason])

    @property
    def outcome(self) -> str:
        return evaluate_source(self).outcome

    @property
    def sweep_allowed(self) -> bool:
        return evaluate_source(self).sweep_allowed

    @property
    def sweep_blocked_reasons(self) -> tuple[str, ...]:
        evaluation = evaluate_source(self)
        return evaluation.reasons if self.full_crawl else ("not_full_crawl", *evaluation.reasons)

    def to_report(self) -> dict[str, Any]:
        report = asdict(self)
        report.pop("observed_ids")  # Aggregate reporting never emits listing IDs or URLs.
        report.update(
            platform=self.platform.value,
            outcome=self.outcome,
            observed=self.previewed,
            processed=self.fetched,
            pages_skipped=self.skipped_pages,
            sweep_allowed=self.sweep_allowed,
            sweep_blocked_reasons=list(self.sweep_blocked_reasons),
        )
        return report

    @property
    def parse_rate(self) -> float:
        """details_parsed_ok / details_attempted. Returns 1.0 when no details were attempted —
        "no attempts" must NOT look like degradation (and the nightly sample gate stops a
        zero-attempt run from ever alerting)."""
        if self.details_attempted == 0:
            return 1.0
        return self.details_parsed_ok / self.details_attempted

    @property
    def coverage_rate(self) -> float | None:
        """Previewed/site_total, or None when the source did not expose a denominator."""
        if self.site_total is None:
            return None
        if self.site_total == 0:
            return 1.0 if self.previewed == 0 else 0.0
        return self.previewed / self.site_total

    @property
    def snapshot_fallback_rate(self) -> float:
        """Incomplete-snapshot fallbacks / snapshots seen; zero when no snapshots were present."""
        if self.snapshots_seen == 0:
            return 0.0
        return self.snapshot_fallbacks / self.snapshots_seen


def _write_source_checkpoint(result: ScrapeResult, path: Path, *, final: bool) -> None:
    """Crash-safe aggregate progress; replay recrawls and relies on normal upsert idempotency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    payload = result.to_report()
    payload.update(checkpoint_at=datetime.now(UTC).isoformat(), final=final)
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


async def scrape_platform(
    scraper: Scraper, *, max_pages: int = 5, max_new: int | None = None,
    download_photos: bool = True, incremental: bool = True, dry_run: bool = False,
    run_id: str | None = None, source_budget_sec: float = 1800,
    item_budget_sec: float = 90, checkpoint_path: Path | None = None,
) -> ScrapeResult:
    """Bound one source and persist progress/final status, including timeout/cancellation.

    Source/listing/publication writes and checkpoints are all disabled in ``dry_run``.
    A restarted job recrawls source presence and safely skips previously persisted details;
    it never replays delivery work or trusts a stale pagination cursor.
    """
    result = ScrapeResult(platform=scraper.platform, run_id=run_id or uuid4().hex,
        full_crawl=not incremental and max_new is None, dry_run=dry_run,
        snapshot_mode=_snapshot_mode(scraper), proxy_metrics=_proxy_metrics_for(scraper))
    scraper.last_scrape_result = result
    path = checkpoint_path or (
        get_settings().data_dir / "state" / "scrape" / f"{scraper.platform.value.lower()}.json"
    )
    last_checkpoint = 0.0
    def progress():
        nonlocal last_checkpoint
        result.pages_attempted = int(getattr(scraper, "_pages_attempted", 0))
        result.pages_failed = int(getattr(scraper, "_pages_failed", 0))
        result.previewed = int(getattr(scraper, "_observed_count", result.previewed))
        result.site_total = getattr(scraper, "_site_total", None)
        if not dry_run and time.monotonic() - last_checkpoint >= 30:
            _write_source_checkpoint(result, path, final=False)
            last_checkpoint = time.monotonic()
    scraper._progress_callback = progress
    try:
        if not dry_run:
            _write_source_checkpoint(result, path, final=False)
        async with asyncio.timeout(max(0.001, source_budget_sec)):
            return await _scrape_platform_impl(scraper, max_pages=max_pages, max_new=max_new,
                download_photos=download_photos, incremental=incremental, dry_run=dry_run,
                run_id=result.run_id, _result=result, item_budget_sec=item_budget_sec,
                checkpoint_path=path)
    except TimeoutError:
        _copy_crawl_evidence(result, scraper, preserve_stop=True)
        result.errors += 1
        result.stop_reason = "source_timeout"
        result.crawl_complete = False
        result.quality_reasons.append("source_timeout")
        return result
    except asyncio.CancelledError:
        _copy_crawl_evidence(result, scraper, preserve_stop=True)
        result.stop_reason = "cancelled"
        result.crawl_complete = False
        result.quality_reasons.append("cancelled")
        raise
    finally:
        scraper._progress_callback = None
        result.proxy_metrics = _proxy_metrics_for(scraper)
        if not dry_run:
            _write_source_checkpoint(result, path, final=True)


async def _scrape_platform_impl(
    scraper: Scraper,
    *,
    max_pages: int = 5,
    max_new: int | None = None,
    download_photos: bool = True,
    incremental: bool = True,
    dry_run: bool = False,
    run_id: str | None = None,
    _result: ScrapeResult | None = None,
    item_budget_sec: float = 90,
    checkpoint_path: Path | None = None,
) -> ScrapeResult:
    """Run one scraping pass for a single platform.

    `incremental=True` (default) preloads existing external_ids for this platform and passes them
    to the scraper, so the scraper can stop paginating after it sees full pages of already-known
    items. First run is full; subsequent runs are fast (1-5 min instead of 6-7 h).
    """
    result = _result or ScrapeResult(
        platform=scraper.platform,
        run_id=run_id or uuid4().hex,
        full_crawl=not incremental and max_new is None,
        dry_run=dry_run,
        snapshot_mode=_snapshot_mode(scraper),
        proxy_metrics=_proxy_metrics_for(scraper),
    )
    scraper._snapshot_shadow_mismatches = 0  # type: ignore[attr-defined]

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

    try:
        previews = await scraper.list_recent(max_pages=max_pages, known_ids=known_ids)
    except Exception as exc:
        result.errors += 1
        result.stop_reason = "list_exception"
        result.quality_reasons.append(type(exc).__name__)
        _copy_crawl_evidence(result, scraper, preserve_stop=True)
        logger.exception("[%s] list crawl failed", scraper.platform.value)
        return result
    result.previewed = len(previews)
    result.observed_ids = {p.external_id for p in previews}
    _copy_crawl_evidence(result, scraper)
    if len(result.observed_ids) != result.previewed:
        result.quality_reasons.append("duplicate_observations")
    logger.info(
        "[%s] list_recent → %d previews (skipped_pages=%d, site_total=%s)",
        scraper.platform.value, len(previews), result.skipped_pages, result.site_total,
    )

    new_count = 0
    if not dry_run and checkpoint_path is not None:
        _write_source_checkpoint(result, checkpoint_path, final=False)
    for index, prev in enumerate(previews):
        if index and index % 25 == 0 and not dry_run and checkpoint_path is not None:
            _write_source_checkpoint(result, checkpoint_path, final=False)
        if max_new is not None and new_count >= max_new:
            result.stop_reason = "item_limit"
            result.crawl_complete = False
            break
        _record_snapshot_telemetry(result, scraper, prev)
        try:
            processor = _evaluate_one if dry_run else _process_one
            inserted, photos, fetched_detail = await asyncio.wait_for(processor(
                scraper,
                prev.url,
                prev.external_id,
                download_photos=download_photos,
                preview=prev,
            ), timeout=max(0.001, item_budget_sec))
        except TimeoutError:
            result.errors += 1
            result.quality_reasons.append("item_timeout")
            continue
        except _DetailFetchError:
            result.details_attempted += 1
            result.errors += 1
            logger.warning(
                "[%s] detail fetch failed external_id=%s", scraper.platform.value, prev.external_id
            )
            continue
        except _EmptyShellError:
            # The all-None-shell guard fired: a detail WAS fetched but the parser couldn't read it
            # (markup change / challenge page). Count it as an attempt that did NOT parse ok, so it
            # drags down parse_rate. Still an error for the existing error counter.
            logger.warning("[%s] empty-shell detail %s", scraper.platform.value, prev.url)
            result.details_attempted += 1
            result.empty_shells += 1
            result.errors += 1
            continue
        except Exception:
            logger.exception("[%s] error processing %s", scraper.platform.value, prev.url)
            result.errors += 1
            continue

        result.fetched += 1
        result.photos_downloaded += photos
        if fetched_detail:
            # A detail page was actually fetched & parsed (re-seen rows skip the fetch entirely).
            result.details_attempted += 1
            result.details_parsed_ok += 1
        if inserted is True:
            result.inserted += 1
            new_count += 1
        elif inserted is False:
            result.updated += 1
    result.snapshot_shadow_mismatches = int(
        getattr(scraper, "_snapshot_shadow_mismatches", 0)
    )
    result.proxy_metrics = _proxy_metrics_for(scraper)
    return result


def _copy_crawl_evidence(
    result: ScrapeResult, scraper: Scraper, *, preserve_stop: bool = False
) -> None:
    result.skipped_pages = int(getattr(scraper, "_skipped_pages", 0))
    result.site_total = getattr(scraper, "_site_total", None)
    result.crawl_complete = bool(getattr(scraper, "_crawl_complete", False))
    result.denominator_trusted = bool(getattr(scraper, "_site_total_trusted", False))
    result.pages_attempted = int(getattr(scraper, "_pages_attempted", 0))
    result.pages_failed = int(getattr(scraper, "_pages_failed", 0))
    if not preserve_stop:
        result.stop_reason = str(getattr(scraper, "_crawl_stop_reason", "unverified"))


async def _evaluate_one(scraper, url, external_id, *, download_photos, preview=None):
    """Network evaluation only: deliberately cannot call persistence/photo helpers."""
    snapshot = _complete_snapshot_for(scraper, preview)
    if _snapshot_mode(scraper) == "active" and snapshot is not None:
        detail, fetched = snapshot, False
    else:
        detail, fetched = await _fetch_processing_detail(scraper, url), True
    if not detail.photo_urls and detail.price_eur is None and detail.title is None:
        raise _EmptyShellError("empty detail during evaluation")
    return None, 0, fetched


class _DetailFetchError(RuntimeError):
    """A real detail transport/parser attempt failed before a usable payload was returned."""


async def _fetch_processing_detail(scraper, url):
    try:
        return await scraper.fetch_detail(url)
    except Exception as exc:
        raise _DetailFetchError(type(exc).__name__) from exc


class _EmptyShellError(RuntimeError):
    """The all-None-shell guard fired in _process_one — a detail fetch "succeeded" on a
    challenge/error page or a payload the parser no longer understands. Typed so scrape_platform
    can count it toward parse_rate (details_attempted++, empty_shells++) distinctly from a generic
    processing error."""


async def _process_one(
    scraper: Scraper,
    url: str,
    external_id: str,
    *,
    download_photos: bool,
    preview: ListingPreview | None = None,
) -> tuple[bool | None, int, bool]:
    """Return (inserted, photos_downloaded, fetched_detail): inserted is True for a new Listing,
    False when an existing row was refreshed, None if skipped. fetched_detail is True only on the
    path that actually called scraper.fetch_detail and parsed a non-shell detail (re-seen rows
    return False — they must not count as a parse attempt)."""
    snapshot = _complete_snapshot_for(scraper, preview)
    snapshot_mode = _snapshot_mode(scraper)

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
        photo_update_needed = False
        async with get_session() as session:
            stmt = select(Listing).where(Listing.id == existing.id)
            row = (await session.execute(stmt)).scalar_one()
            row.last_seen_at = datetime.now(timezone.utc)
            row.absence_streak = 0
            row.absence_first_seen_at = row.absence_last_seen_at = row.absence_last_run_id = None
            row.deactivated_at = None
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
            if snapshot_mode == "active" and snapshot is not None:
                _merge_snapshot_scalars(row, snapshot)
                photo_update_needed = row.photo_urls != snapshot.photo_urls

        if photo_update_needed and download_photos and snapshot is not None:
            photo_paths, photo_hashes, embeddings_blob = await _download_photos(snapshot)
            # Never replace a complete existing album with a partial download. The next scrape
            # will retry the same snapshot while preserving the old URLs/hashes/embeddings.
            if len(photo_paths) == len(snapshot.photo_urls):
                async with get_session() as session:
                    row = (await session.execute(select(Listing).where(Listing.id == existing.id))).scalar_one()
                    row.photo_urls = snapshot.photo_urls
                    row.photo_phashes = photo_hashes
                    row.photo_embeddings = embeddings_blob
                return False, len(photo_paths), False
            logger.warning(
                "[%s] snapshot photo swap skipped after partial download external_id=%s",
                scraper.platform.value,
                external_id,
            )
        return False, 0, False

    attempted_at = datetime.now(UTC)
    if snapshot_mode == "active" and snapshot is not None:
        detail = snapshot
        fetched_detail = False
    else:
        detail = await _fetch_processing_detail(scraper, url)
        fetched_detail = True
        if (
            snapshot_mode == "shadow"
            and snapshot is not None
            and _log_snapshot_mismatch(snapshot, detail)
        ):
            scraper._snapshot_shadow_mismatches = (  # type: ignore[attr-defined]
                int(getattr(scraper, "_snapshot_shadow_mismatches", 0)) + 1
            )

    if not detail.photo_urls and detail.price_eur is None and detail.title is None:
        # All-None shell — the detail fetch "succeeded" on a challenge/error page or a payload
        # the parser no longer understands. Inserting it would freeze the listing forever
        # (existing rows are never detail-refreshed): no photos → invisible to the matcher,
        # while its external_id blocks any future re-fetch. Fail so the next walk retries.
        raise _EmptyShellError(f"empty detail payload for {url} — not inserting a dead row")

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
        await session.flush()
        if fetched_detail:
            evidence = assess_detail(
                detail, ready_photo_count=_usable_photo_count(photo_paths, photo_hashes),
            )
            await record_attempt(session, listing.id, evidence, attempted_at=attempted_at)
    return True, len(photo_paths), fetched_detail


def _snapshot_mode(scraper: Scraper) -> str:
    """Read the Nekretnine mode without changing behavior for the other platforms."""
    if scraper.platform != Platform.NEKRETNINE:
        return "legacy"
    return get_settings().nekretnine_list_snapshot_mode


def _proxy_metrics_for(scraper: Scraper) -> dict[str, int]:
    """Copy only integer, secret-safe counters exposed by a scraper."""
    metrics = getattr(scraper, "proxy_metrics", None)
    if not isinstance(metrics, Mapping):
        return {}
    return {
        str(name): value
        for name, value in metrics.items()
        if isinstance(name, str) and isinstance(value, int)
    }


def _record_snapshot_telemetry(
    result: ScrapeResult,
    scraper: Scraper,
    preview: ListingPreview,
) -> None:
    if scraper.platform != Platform.NEKRETNINE or preview.snapshot is None:
        return
    result.snapshots_seen += 1
    complete = _complete_snapshot_for(scraper, preview) is not None
    if complete:
        result.snapshots_complete += 1
        if result.snapshot_mode == "active":
            result.snapshot_active_uses += 1
    else:
        result.snapshots_incomplete += 1
        if result.snapshot_mode in {"shadow", "active"}:
            result.snapshot_fallbacks += 1


def _complete_snapshot_for(
    scraper: Scraper, preview: ListingPreview | None
) -> ListingDetail | None:
    if preview is None or scraper.platform != Platform.NEKRETNINE or preview.snapshot is None:
        return None
    from src.scrapers.nekretnine import NekretnineScraper

    return preview.snapshot if NekretnineScraper.is_snapshot_complete(preview.snapshot) else None


def _merge_snapshot_scalars(row: Listing, snapshot: ListingDetail) -> None:
    """Merge only non-null snapshot values; an incomplete upstream never clears DB truth."""
    for field_name in (
        "price_eur",
        "area_m2",
        "rooms",
        "district",
        "address_raw",
        "lat",
        "lon",
        "title",
    ):
        value = getattr(snapshot, field_name)
        if value is not None:
            setattr(row, field_name, value)
    if snapshot.url:
        row.url = snapshot.url


def _log_snapshot_mismatch(snapshot: ListingDetail, detail: ListingDetail) -> bool:
    mismatches = mismatch_fields(snapshot, detail)
    if mismatches:
        logger.warning(
            "[nekretnine] snapshot shadow mismatch external_id=%s fields=%s",
            snapshot.external_id,
            ",".join(mismatches),
        )
    return bool(mismatches)


def _make_side_client(
    *,
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | float = 30.0,
    use_proxy: bool = False,
) -> httpx.AsyncClient:
    """httpx client for the orchestrator's non-scraper side traffic — photo downloads and the
    verify-links HEAD sweep.

    Proxy use is explicit at each call site and reserved for Nekretnine. Other platforms and their
    CDN/HEAD traffic stay on the direct VPS route so an unavailable or exhausted proxy cannot take
    down the weekly refresh.
    """
    proxy = get_settings().scraper_proxy_url or None if use_proxy else None
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers=headers,
        proxy=proxy,
    )


async def _download_photos(
    detail: ListingDetail,
    *,
    target_dir: Path | None = None,
) -> tuple[list[Path], list[str], bytes | None]:
    """Download every photo, compute pHash, batch CLIP embeddings. Returns (paths, phashes, embeddings_blob)."""
    settings = get_settings()
    settings.ensure_dirs()
    target_dir = target_dir or (
        settings.listings_photos_dir / detail.platform.value / detail.external_id
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    headers = _photo_request_headers(detail.url)
    # Parallel download — up to 4 concurrent fetches per listing. Major speed-up: a 10-photo
    # listing drops from ~5s sequential to ~1.5s. CDN rate limits remain respected because
    # different listings still run serially in the outer loop.
    sem = asyncio.Semaphore(4)
    results: list[tuple[int, Path, str] | None] = [None] * len(detail.photo_urls)

    # Nekretnine's CDN is not behind the DataDome page wall: direct VPS requests return 200
    # while the listing pages return 403.  Keep those large immutable image transfers off the
    # metered residential proxy; other platforms retain the configured proxy path.
    direct_nekretnine_cdn = detail.platform == Platform.NEKRETNINE and bool(detail.photo_urls) and all(
        urlparse(url).hostname == "pic.nekretnine.rs" for url in detail.photo_urls
    )
    async with _make_side_client(
        headers=headers,
        timeout=httpx.Timeout(30.0, connect=10.0),
        use_proxy=detail.platform == Platform.NEKRETNINE and not direct_nekretnine_cdn,
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
    from src.config import get_settings
    from src.scrapers.base import DEFAULT_USER_AGENT

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
    marked_inactive: int = 0
    absence_recorded: int = 0
    would_mark_inactive: int = 0
    blocked_reasons: tuple[str, ...] = ()
    dry_run: bool = False


async def mark_stale_inactive(
    *, inactive_after_days: int = 10, platform: Platform | None = None,
    source_result: ScrapeResult | None = None, dry_run: bool = False,
    now: datetime | None = None, max_deactivations: int = 100,
) -> StaleSweepResult:
    """Confirm source absence after consecutive quality weekly crawls; never hide public cards.

    ``inactive_after_days`` is retained for CLI compatibility, but row age is no longer evidence.
    Missing/partial/untrusted source evidence is an unconditional no-write exit.
    """
    report = StaleSweepResult(platform=platform, dry_run=dry_run)
    if source_result is None:
        report.blocked_reasons = ("missing_source_result",)
        return report
    platform = platform or source_result.platform
    report.platform = platform
    if source_result.platform != platform or not source_result.sweep_allowed:
        report.blocked_reasons = source_result.sweep_blocked_reasons or ("source_mismatch",)
        return report
    if source_result.dry_run and not dry_run:
        report.blocked_reasons = ("evaluation_result_cannot_mutate",)
        return report
    now = now or datetime.now(UTC)
    async with get_session() as session:
        # Scalar projection avoids loading the photo embedding blobs for a full platform.
        rows = (
            await session.execute(
                select(
                    Listing.id,
                    Listing.external_id,
                    Listing.is_active,
                    Listing.absence_streak,
                    Listing.absence_first_seen_at,
                    Listing.absence_last_seen_at,
                    Listing.absence_last_run_id,
                ).where(Listing.platform == platform, Listing.is_active.is_(True))
            )
        ).all()
        decisions = []
        for row in rows:
            decision = evaluate_absence(
                observed=row.external_id in source_result.observed_ids,
                streak=row.absence_streak or 0, first_seen_at=row.absence_first_seen_at,
                last_seen_at=row.absence_last_seen_at, last_run_id=row.absence_last_run_id,
                run_id=source_result.run_id, now=now,
            )
            decisions.append((row, decision))
            if decision.deactivate and row.is_active:
                report.would_mark_inactive += 1
            if decision.reason not in {"already_evaluated", "confirmation_too_soon", "observed"}:
                report.absence_recorded += 1
        if report.would_mark_inactive > max(0, max_deactivations):
            report.blocked_reasons = ("deactivation_cap_exceeded",)
            return report
        if dry_run:
            return report
        for row, decision in decisions:
            if (
                decision.reason == "observed"
                and not row.absence_streak
                and row.absence_last_run_id is None
            ):
                continue
            if decision.reason in {"already_evaluated", "confirmation_too_soon"}:
                continue
            values = dict(
                absence_streak=decision.streak,
                absence_first_seen_at=decision.first_seen_at,
                absence_last_seen_at=decision.last_seen_at,
                absence_last_run_id=decision.last_run_id,
            )
            if decision.deactivate and row.is_active:
                values["is_active"] = False
                values["deactivated_at"] = now
                report.marked_inactive += 1
            await session.execute(update(Listing).where(Listing.id == row.id).values(**values))
    return report


# ------------------------------------------------------------------ liveness sweep (A3)


@dataclass
class LinkVerifyStats:
    """Outcome of a liveness sweep (`check-links` CLI / nightly verify-links step).

    `checked` rows we issued a HEAD for; `deactivated` flipped is_active=False on a definitive
    gone-status (404/410); `errors` inconclusive responses (timeouts, 5xx, anti-bot 403/405,
    network) that did NOT deactivate; `skipped` rows left unchecked when the deadline ran out.
    """

    checked: int = 0
    deactivated: int = 0
    errors: int = 0
    skipped: int = 0
    # X2: public cards auto-hidden because their listing 404/410'd (is_public was True).
    site_listings_hidden: int = 0


# A HEAD that comes back 404/410 means the listing is definitively gone — deactivate it. Every
# other status (incl. anti-bot 403/405 from Cloudflare-fronted platforms like halooglasi, and
# 5xx server hiccups) is inconclusive: we must never deactivate a live listing on a transient
# or anti-bot response.
_DEAD_LINK_STATUSES = frozenset({404, 410})


async def hide_site_listings_for_listing(session: Any, listing_id: int, *,
                                         member_status_by_id: Mapping[int, str] | None = None,
                                         remaining_hides: int | None = None,
                                         dry_run: bool = False) -> int:
    """Compatibility entry point: every automatic hide now goes through the common policy."""
    from src.site.publication_policy import apply_publication_policy

    maximum = get_settings().site_liveness_max_hides if remaining_hides is None else remaining_hides
    ids = (
        (
            await session.execute(
                select(SiteListing.id)
                .join(Match, Match.id == SiteListing.match_id)
                .where(Match.listing_id == listing_id, SiteListing.is_public.is_(True))
                .order_by(SiteListing.id)
            )
        )
        .scalars()
        .all()
    )
    hidden = 0
    for card_id in ids:
        _decision, count = await apply_publication_policy(
            session, card_id, member_status_by_id=member_status_by_id or {},
            remaining_hides=maximum-hidden, dry_run=dry_run,
        )
        hidden += count
    return hidden


async def verify_active_links(
    *,
    limit: int | None = None,
    deadline: float | None = None,
    platform: Platform | None = None,
    client: httpx.AsyncClient | None = None,
    dry_run: bool = False,
) -> LinkVerifyStats:
    """Verify ACTIVE matched sources through bounded payload-aware GET/browser probes.

    Definitive 404/410 records source removal, then the common policy controls publication.

    Selection: ACTIVE listings whose id is referenced by some PENDING or PUBLISHED `Match`.
    Optionally filtered by `platform`; capped by `limit`.

    Production uses one bounded source session at a time, with the source's configured route.
    Only verified payloads establish ACTIVE; only 404/410 deactivate. All uncertain outcomes
    are recorded with a retry time. Explicit HTTP client injection supports offline HEAD tests.

    `deadline` is a `time.monotonic()` target shared with the heal/rolling refetch steps: once
    it passes, all still-unchecked rows are counted in `skipped` and the sweep stops cleanly.

    `client` is injectable for tests (pass a MockTransport-backed AsyncClient). Production does
    not use the legacy side-client HEAD route.
    """
    checked_at = datetime.now(UTC)
    async with get_session() as s:
        stmt = (
            select(Listing.id, Listing.platform, Listing.url)
            .where(
                Listing.is_active.is_(True),
                select(Match.id)
                .where(
                    Match.listing_id == Listing.id,
                    Match.status.in_((MatchStatus.PENDING, MatchStatus.PUBLISHED)),
                )
                .exists(),
            )
            .where(
                (Listing.link_next_retry_at.is_(None)) | (Listing.link_next_retry_at <= checked_at)
            )
            .order_by(Listing.link_checked_at.asc().nullsfirst(), Listing.id)
        )
        if platform is not None:
            stmt = stmt.where(Listing.platform == platform)
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await s.execute(stmt)).all()

    stats = LinkVerifyStats()
    if not rows:
        logger.info("[verify-links] no active matched listings to check (platform=%s)",
                    platform.value if platform else "all")
        return stats

    observed_status: dict[int, str] = {}
    dead_ids: set[int] = set()

    async def _record_check(row, status: str, *, definitive_alive: bool = False):
        observed_status[row.id] = status
        if dry_run:
            return
        values = dict(
            link_checked_at=checked_at,
            link_status=status,
            link_next_retry_at=checked_at + timedelta(hours=6)
            if status == "INCONCLUSIVE"
            else None,
        )
        if definitive_alive:
            values["link_succeeded_at"] = checked_at
        if status == "DEAD":
            values.update(is_active=False, deactivated_at=checked_at, link_succeeded_at=checked_at)
        async with get_session() as s:
            await s.execute(update(Listing).where(Listing.id == row.id).values(**values))

    async def _check_one(row: Any, probe: SourceProbeSession | None = None) -> None:
        if probe is not None:
            observation = await probe.probe(row.url)
            await _record_check(
                row, observation.status, definitive_alive=observation.status == "ACTIVE"
            )
            if observation.status == "DEAD":
                dead_ids.add(row.id)
                stats.deactivated += 1
            elif observation.errored:
                stats.errors += 1
            return
        try:
            assert client is not None
            resp = await client.head(row.url, timeout=10.0)
        except Exception as e:
            stats.errors += 1
            await _record_check(row, "INCONCLUSIVE")
            logger.warning("[verify-links] #%s inconclusive: %s", row.id, type(e).__name__)
            return
        if resp.status_code in _DEAD_LINK_STATUSES:
            await _record_check(row, "DEAD")
            dead_ids.add(row.id)
            stats.deactivated += 1
        elif resp.status_code >= 400:
            stats.errors += 1
            await _record_check(row, "INCONCLUSIVE")
        else:
            # HEAD 200 is reachable, not proof of a valid listing payload (W3 probe owns that).
            await _record_check(row, "INCONCLUSIVE")

    # 4 workers pull from a shared cursor, each re-checking the deadline BEFORE taking a row, so
    # the budget (shared with heal/rolling) actually caps wall-clock mid-sweep — not just at
    # task-creation time. The cursor read+advance has no await between, so it's race-free on the
    # single-threaded event loop; anything left when the deadline trips is counted as skipped.
    cursor = 0

    async def _worker() -> None:
        nonlocal cursor
        while True:
            if deadline is not None and time.monotonic() > deadline:
                return
            if cursor >= len(rows):
                return
            row = rows[cursor]
            cursor += 1
            stats.checked += 1
            await _check_one(row)

    if client is not None:
        await asyncio.gather(*(_worker() for _ in range(min(4, len(rows)))))
    else:
        # Never hold two platform leases at once, avoiding a cross-entrypoint lock cycle.
        for source in sorted({row.platform for row in rows}, key=lambda value: value.value):
            if deadline is not None and time.monotonic() > deadline:
                break
            async with SourceProbeSession(source) as probe:
                for row in (row for row in rows if row.platform == source):
                    if deadline is not None and time.monotonic() > deadline:
                        break
                    stats.checked += 1
                    await _check_one(row, probe)
    stats.skipped = len(rows) - stats.checked

    if dead_ids and not dry_run:
        # All source observations are complete before one serialized cap-controlled policy pass.
        async with get_session() as s:
            for listing_id in sorted(dead_ids):
                stats.site_listings_hidden += await hide_site_listings_for_listing(
                    s, listing_id, member_status_by_id=observed_status,
                    remaining_hides=get_settings().site_liveness_max_hides-stats.site_listings_hidden,
                )

    logger.info(
        "[verify-links] checked=%d deactivated=%d errors=%d skipped=%d (platform=%s)",
        stats.checked, stats.deactivated, stats.errors, stats.skipped,
        platform.value if platform else "all",
    )
    return stats


# ------------------------------------------------------------------ auto-publish backfill (X1)


async def auto_publish_new_listings(
    *, since_hours: int = 24, limit: int | None = None
) -> int:
    """X1 backfill: expose recently-delivered matches that lack a public card.

    Selects PUBLISHED matches created within `since_hours` that HAVE a PublishedPost (i.e.
    actually sent to the channel) AND no SiteListing overlay yet, then runs the shared
    auto_publish_site_listing helper for each (creating a public card with parsed flags).
    Returns the number of cards created. A safety net for the delivery-path hook: anything
    the hot-path hook missed (kill switch was on, a helper exception, an older deploy) gets
    swept up here. Already-exposed matches are skipped (idempotent).
    """
    from src.admin.publish import auto_publish_site_listing

    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    async with get_session() as s:
        stmt = (
            select(Match.id)
            .where(
                Match.status == MatchStatus.PUBLISHED,
                Match.created_at >= cutoff,
                select(PublishedPost.id)
                .where(PublishedPost.match_id == Match.id)
                .exists(),
                ~select(SiteListing.id)
                .where(SiteListing.match_id == Match.id)
                .exists(),
            )
            .order_by(Match.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        match_ids = [row[0] for row in (await s.execute(stmt)).all()]

        created_count = 0
        for match_id in match_ids:
            _row, created = await auto_publish_site_listing(s, match_id)
            if created:
                created_count += 1
        await s.commit()

    logger.info(
        "[auto-publish] exposed %d new public card(s) (since_hours=%d)",
        created_count, since_hours,
    )
    return created_count


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
    """Accepted content updates and verified full refreshes are separate counters."""

    selected: int = 0
    updated: int = 0
    verified: int = 0
    photos: int = 0
    errors: int = 0
    deactivated: int = 0
    skipped: int = 0
    source_errors: dict[str, str] = field(default_factory=dict)
    price_changes: list[PriceChange] = field(default_factory=list)


@dataclass
class _RefetchOutcome:
    photos_added: int
    old_price: int | None
    new_price: int | None
    evidence: RefreshEvidence


def _write_refresh_state(name: str, payload: dict[str, Any]) -> None:
    path = get_settings().data_dir / "state" / "freshness" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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
    """Refresh with a reserved time share for every selected source, including browser startup.

    Every real attempt records evidence and retry state; the legacy touch flags remain
    accepted for existing callers. Only complete fields and verified local photos advance
    details_succeeded_at. An external cancellation records the in-flight failed attempt
    and a final aggregate checkpoint before propagating to the command runner.
    """
    if scrapers is None:
        from src.scrapers import SCRAPERS

        scrapers = SCRAPERS
    stats = DetailRefetchStats(selected=len(rows))
    candidates = tuple(RefreshCandidate(row.id, row.platform, row.url) for row in rows)
    order = tuple(dict.fromkeys(row.platform for row in rows))
    plan = FairRefreshPlan(candidates, order, 0, len(rows), 0)
    budget = (
        deadline - time.monotonic()
        if deadline is not None
        else get_settings().nightly_refetch_budget_sec
    )
    run_id = uuid4().hex
    stop_reason = "running"
    next_fetch_at = 0.0
    attempted_ids: set[int] = set()

    def checkpoint(*, final: bool) -> None:
        _write_refresh_state("refetch", {
            "run_id": run_id, "checkpoint_at": datetime.now(UTC).isoformat(),
            "final": final, "stop_reason": stop_reason, **asdict(stats),
            "price_changes": len(stats.price_changes),
        })

    def account(row: RefreshCandidate, outcome: _RefetchOutcome) -> None:
        stats.updated += 1
        stats.verified += int(outcome.evidence.succeeded)
        stats.errors += int(not outcome.evidence.succeeded)
        stats.photos += outcome.photos_added
        if outcome.old_price != outcome.new_price:
            stats.price_changes.append(
                PriceChange(row.id, outcome.old_price, outcome.new_price)
            )

    async def refresh(row: RefreshCandidate, scraper: Scraper) -> CallbackResult:
        nonlocal next_fetch_at
        # Delay before starting a real attempt: cancellation while waiting is not a fetch.
        if sleep_between and next_fetch_at > time.monotonic():
            await asyncio.sleep(next_fetch_at - time.monotonic())
        attempted_at = datetime.now(UTC)
        attempted_ids.add(row.id)
        try:
            outcome = await _refetch_one_listing(
                scraper, row.id, row.url, download_photos=download_photos,
                touch_refreshed_at=touch_refreshed_at, attempted_at=attempted_at,
            )
            if outcome is None:
                stats.skipped += 1
                return CallbackResult(RefreshEvidence.failed("listing_deleted"))
            account(row, outcome)
            if progress is not None:
                with suppress(Exception):
                    progress(stats, row, None if outcome.evidence.succeeded else
                             RuntimeError(outcome.evidence.reason))
            return CallbackResult(outcome.evidence, outcome)
        except BaseException as error:
            committed_outcome = getattr(error, "committed_refresh_outcome", None)
            if isinstance(error, asyncio.CancelledError) and isinstance(
                committed_outcome, _RefetchOutcome,
            ):
                account(row, committed_outcome)
                raise
            evidence = RefreshEvidence.failed(type(error).__name__)
            stats.errors += 1
            status = getattr(getattr(error, "response", None), "status_code", None)
            try:
                async with get_session() as session:
                    if status in (404, 410):
                        now = datetime.now(UTC)
                        await session.execute(update(Listing).where(Listing.id == row.id).values(
                            is_active=False, deactivated_at=now, link_status="DEAD",
                            link_checked_at=now, link_next_retry_at=None,
                        ))
                        stats.deactivated += 1
                    await record_attempt(session, row.id, evidence, attempted_at=attempted_at)
            except BaseException as persistence_error:
                if isinstance(error, asyncio.CancelledError):
                    raise error from persistence_error
                raise
            if isinstance(error, Exception) and progress is not None:
                with suppress(Exception):
                    progress(stats, row, error)
            raise
        finally:
            next_fetch_at = time.monotonic() + max(0, sleep_between)
            checkpoint(final=False)

    checkpoint(final=False)
    try:
        if not rows or budget <= 0:
            stats.skipped = len(rows)
            stop_reason = "budget_exhausted" if rows else "empty"
            return stats
        execution = await run_fair_refresh(
            plan, refresh, budget_sec=budget,
            item_timeout_sec=get_settings().refetch_per_fetch_timeout_sec,
            context_factory=lambda platform: scrapers[platform](),
        )
        stats.source_errors = dict(execution.source_errors)
        stats.skipped += sum(item.row.id not in attempted_ids for item in execution.items)
        stop_reason = (
            "degraded" if stats.errors or stats.skipped or stats.source_errors else "completed"
        )
        return stats
    except asyncio.CancelledError:
        stop_reason = "cancelled"
        raise
    except BaseException:
        stop_reason = "failed"
        raise
    finally:
        checkpoint(final=True)


def _usable_photo_count(paths: Sequence[Path], hashes: Sequence[str]) -> int:
    """Count readable image/hash pairs; an index or a remote URL alone is not evidence."""
    from PIL import Image

    ready = 0
    for path, digest in zip(paths, hashes, strict=False):
        if not isinstance(digest, str) or len(digest) != 16:
            continue
        try:
            int(digest, 16)
            with Image.open(path) as photo:
                photo.verify()
            ready += 1
        except (OSError, ValueError, SyntaxError):
            continue
    return ready


async def _refetch_one_listing(
    scraper: Scraper,
    listing_id: int,
    url: str,
    *,
    download_photos: bool,
    touch_refreshed_at: bool,
    attempted_at: datetime | None = None,
) -> _RefetchOutcome | None:
    attempted_at = attempted_at or datetime.now(UTC)
    detail = await asyncio.wait_for(
        scraper.fetch_detail(url), timeout=get_settings().refetch_per_fetch_timeout_sec,
    )
    if not detail.photo_urls and detail.price_eur is None and detail.title is None:
        raise RuntimeError("empty_detail")
    async with get_session() as session:
        current = (await session.execute(select(
            Listing.platform, Listing.external_id, Listing.photo_urls,
            Listing.photo_phashes, Listing.price_eur,
        ).where(Listing.id == listing_id))).one_or_none()
    if current is None:
        return None
    if detail.platform != current.platform or detail.external_id != current.external_id:
        raise RuntimeError("detail_identity_mismatch")
    old_urls = list(current.photo_urls or [])
    old_hashes = list(current.photo_phashes or [])
    target = get_settings().listings_photos_dir / current.platform.value / current.external_id
    old_paths = [target / f"{index:03d}{_guess_extension(photo_url)}"
                 for index, photo_url in enumerate(old_urls)]
    ready = _usable_photo_count(old_paths, old_hashes) if detail.photo_urls == old_urls else 0
    need_photos = bool(download_photos and detail.photo_urls and ready < len(detail.photo_urls))
    hashes: list[str] = []
    embeddings: bytes | None = None
    stage = target.with_name(f".{target.name}.pending-{uuid4().hex}")
    backup = target.with_name(f".{target.name}.previous-{uuid4().hex}")
    promoted = False
    committed = False
    try:
        if need_photos:
            paths, hashes, embeddings = await _download_photos(detail, target_dir=stage)
            ready = _usable_photo_count(paths, hashes)
            if len(hashes) != len(detail.photo_urls) or ready != len(detail.photo_urls):
                raise RuntimeError("incomplete_photos")
            # Validate the canonical index layout too before touching the accepted album.
            expected = [stage / f"{index:03d}{_guess_extension(photo_url)}"
                        for index, photo_url in enumerate(detail.photo_urls)]
            if _usable_photo_count(expected, hashes) != len(detail.photo_urls):
                raise RuntimeError("incomplete_photo_layout")
        evidence = assess_detail(detail, ready_photo_count=ready)
        async def accept() -> int | None:
            nonlocal promoted, committed
            async with get_session() as session:
                listing = await session.get(Listing, listing_id)
                if listing is None:
                    return None
                if need_photos:
                    if target.exists():
                        target.rename(backup)
                    stage.rename(target)
                    promoted = True
                    listing.photo_urls = list(detail.photo_urls)
                    listing.photo_phashes = hashes
                    listing.photo_embeddings = embeddings
                # Partial upstream fields must not erase previously accepted content.
                for name in ("price_eur", "area_m2", "rooms", "district", "address_raw", "lat",
                             "lon", "title", "description"):
                    value = getattr(detail, name)
                    if value is not None and name not in evidence.missing_fields:
                        setattr(listing, name, value)
                if evidence.succeeded:
                    now = datetime.now(UTC)
                    listing.is_active = True
                    listing.last_seen_at = now
                    listing.deactivated_at = None
                    listing.absence_streak = 0
                    listing.absence_first_seen_at = None
                    listing.absence_last_seen_at = None
                    listing.absence_last_run_id = None
                    listing.link_status = "ACTIVE"
                    listing.link_checked_at = listing.link_succeeded_at = now
                    listing.link_next_retry_at = None
                listing.raw_payload = detail.raw_payload
                await record_attempt(session, listing_id, evidence, attempted_at=attempted_at)
                new_price = listing.price_eur
                # Acknowledged commit precedes session-close awaits. The caller shields only
                # this local acceptance task and settles it before rollback on cancellation.
                await session.commit()
                committed = True
            return new_price

        transaction = asyncio.create_task(accept())
        try:
            new_price = await asyncio.shield(transaction)
        except asyncio.CancelledError as cancellation:
            # SQLite may have committed on its worker thread already. Resolve the same
            # transaction task before deciding whether restoring previous files is valid.
            while not transaction.done():
                try:
                    await asyncio.shield(transaction)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                new_price = transaction.result()
            except BaseException as acceptance_error:
                raise cancellation from acceptance_error
            if committed:
                # Keep the exact CancelledError type: asyncio.timeout recognizes it to
                # convert a source/item deadline into TimeoutError and continue its queue.
                cancelled = asyncio.CancelledError("refresh_committed_before_cancellation")
                cancelled.committed_refresh_outcome = _RefetchOutcome(
                    len(hashes), current.price_eur, new_price, evidence,
                )
                raise cancelled from None
            raise
        if not committed:
            return None
        return _RefetchOutcome(len(hashes), current.price_eur, new_price, evidence)
    finally:
        if not committed:
            if promoted:
                shutil.rmtree(target, ignore_errors=True)
            if backup.exists() and not target.exists():
                backup.rename(target)
        if committed:
            shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)


@dataclass
class HealResult:
    selected: int
    fixed: int
    remaining: int
    errors: int
    skipped: int = 0


_BROKEN_LISTING = (Listing.is_active.is_(True), Listing.photo_phashes == [])


async def _freshness_queue(
    *, name: str, limit: int, scrapers: Mapping[Platform, type[Scraper]], only_broken: bool,
) -> FairRefreshPlan:
    path = get_settings().data_dir / "state" / "freshness" / f"{name}_cursor.json"
    try:
        cursor = int(json.loads(path.read_text())["next_cursor"])
    except (OSError, ValueError, KeyError, TypeError):
        cursor = 0
    async with get_session() as session:
        inventory = await load_candidates(session, platforms=scrapers, only_broken=only_broken)
    plan = select_fair(inventory, limit=limit, source_cursor=cursor)
    _write_refresh_state(f"{name}_cursor", {
        "next_cursor": plan.next_cursor, "selected": len(plan.rows),
        "eligible": plan.eligible, "deferred": plan.deferred,
        "checkpoint_at": datetime.now(UTC).isoformat(),
    })
    return plan


async def heal_broken_listings(
    *,
    limit: int = 500,
    scrapers: Mapping[Platform, type[Scraper]] | None = None,
    deadline: float | None = None,
) -> HealResult:
    """Repair due broken rows fairly; only verified full repairs count as fixed."""
    if scrapers is None:
        from src.scrapers import SCRAPERS

        scrapers = SCRAPERS
    plan = await _freshness_queue(name="heal", limit=limit, scrapers=scrapers, only_broken=True)
    stats = await refetch_listing_details(plan.rows, scrapers=scrapers, deadline=deadline)
    async with get_session() as session:
        remaining = len(await load_candidates(session, platforms=scrapers, only_broken=True))
    logger.info("[heal] selected=%d fixed=%d remaining=%d errors=%d skipped=%d",
                len(plan.rows), stats.verified, remaining, stats.errors, stats.skipped)
    return HealResult(len(plan.rows), stats.verified, remaining, stats.errors, stats.skipped)


@dataclass
class RollingRefreshResult:
    selected: int
    refreshed: int
    errors: int
    price_changes: list[PriceChange]
    skipped: int = 0


async def rolling_detail_refresh(
    *,
    limit: int = 200,
    scrapers: Mapping[Platform, type[Scraper]] | None = None,
    deadline: float | None = None,
) -> RollingRefreshResult:
    """Public, broken, oldest-success priority within fair, retry-aware source queues."""
    if scrapers is None:
        from src.scrapers import SCRAPERS

        scrapers = SCRAPERS
    plan = await _freshness_queue(name="rolling", limit=limit, scrapers=scrapers, only_broken=False)
    stats = await refetch_listing_details(plan.rows, scrapers=scrapers, deadline=deadline)
    logger.info("[rolling-refresh] selected=%d verified=%d errors=%d skipped=%d",
                len(plan.rows), stats.verified, stats.errors, stats.skipped)
    return RollingRefreshResult(
        len(plan.rows), stats.verified, stats.errors, stats.price_changes, stats.skipped,
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
