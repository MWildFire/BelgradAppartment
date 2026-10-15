"""Clean copies for photos of public site listings (used with WATERMARK_SITE_USE_CLEAN=1).

`otz wm-clean-published` runs this once by hand; the bot daemon (`otz bot`) repeats
`sweep_once` every SWEEP_INTERVAL_SECONDS, so newly published apartments get clean copies and
fresh site variants without manual steps. The heavy work stays in the bot process (its unit has
Nice=10 and MemoryMax) and goes through the caller's `run` (wm_bot.run_exclusive: one CPU job
at a time, a few photos per call, so /clean requests are not blocked for long).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.config import get_settings
from src.watermark.pipeline import NotAnImageError
from src.watermark.store import (
    clean_or_original,
    clean_path_for,
    ensure_clean,
    is_current,
    resolve_stored,
    sidecar_for,
    site_marker_for,
)

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 600
SWEEP_BATCH = 4
SITE_VARIANT_BATCH = 20

Runner = Callable[..., Awaitable[Any]]


async def published_photo_paths() -> list[str]:
    """InboundPost.photo_paths of every public SiteListing whose match is PUBLISHED."""
    from sqlalchemy import select

    from src.db import get_session
    from src.db.models import InboundPost, Match, MatchStatus, SiteListing

    async with get_session() as session:
        rows = (
            await session.execute(
                select(InboundPost.photo_paths)
                .join(Match, Match.inbound_post_id == InboundPost.id)
                .join(SiteListing, SiteListing.match_id == Match.id)
                .where(SiteListing.is_public.is_(True), Match.status == MatchStatus.PUBLISHED)
            )
        ).scalars().all()
    return [str(p) for paths in rows for p in (paths or []) if p]


def pending(paths: list[str]) -> list[str]:
    """Stored paths whose clean copy is missing or outdated (cheap: stat + sidecar read)."""
    out = []
    for stored in paths:
        original = resolve_stored(stored)
        if original is not None and not is_current(original):
            out.append(stored)
    return out


def clean_batch(paths: list[str], *, force: bool = False) -> tuple[int, int]:
    """ensure_clean for each path -> (photos that had marks, photos missing on disk).

    A file that is not a readable image is logged and skipped: one broken photo must not stop
    the rest of the sweep (it would fail the same way on every run).
    """
    changed = missing = 0
    for stored in paths:
        try:
            result = ensure_clean(stored, force=force)
        except NotAnImageError:
            logger.warning("[watermark] not a readable image, skipped: %s", stored)
            continue
        if result is None:
            missing += 1
        elif result.clean is not None:
            changed += 1
    return changed, missing


def site_variant_files(stored: str) -> list[Path]:
    """Site card/detail files of one photo; empty when the photo is not under photos_root()."""
    from src.site.photos import VARIANT_WIDTHS, photos_root

    original = resolve_stored(stored)
    if original is None:
        return []
    root = photos_root()
    try:
        rel = original.relative_to(root).as_posix()
    except ValueError:
        return []
    return [root / f"{rel}.{kind}.webp" for kind in VARIANT_WIDTHS]


def _variant_state(stored: str) -> tuple[Path, Path] | None:
    """(copy location, freshness source), including a current decision to keep originals."""
    original = resolve_stored(stored)
    if original is None:
        return None
    try:
        target = clean_path_for(original)
        if target is None or not is_current(original):
            return None
        source = clean_or_original(original)
        # When all marks are skipped the old clean copy disappears. Its new sidecar still
        # invalidates previews, so the next sweep restores them from the original.
        return target, sidecar_for(target) if source == original else source
    except RuntimeError:
        return None


def needs_site_variants(stored: str) -> bool:
    """Site variants have not been rebuilt for the current clean/original decision."""
    state = _variant_state(stored)
    if state is None:
        return False
    target, source = state
    try:
        marker = site_marker_for(target)
        variants = site_variant_files(stored)
        if not variants:
            return False
        return (
            not all(variant.is_file() for variant in variants)
            or not marker.exists()
            or marker.stat().st_mtime_ns < source.stat().st_mtime_ns
        )
    except RuntimeError:
        return False
    except OSError:
        return True


def rebuild_site_variants(paths: list[str]) -> int:
    """Rebuild the site variants that are older than the clean copy; returns the photo count.

    process_photo alone misses a variant built from the original before the clean copy existed,
    so the old variants are deleted first and a marker records each rebuild. No-op while
    WATERMARK_SITE_USE_CLEAN is off.
    """
    from src.site.photos import generate_for_stored_paths

    if not get_settings().watermark_site_use_clean:
        return 0
    todo = [stored for stored in paths if needs_site_variants(stored)]
    for stored in todo:
        for variant in site_variant_files(stored):
            variant.unlink(missing_ok=True)
    generate_for_stored_paths(todo)
    rebuilt = 0
    for stored in todo:
        state = _variant_state(stored)
        variants = site_variant_files(stored)
        if state is not None and variants and all(variant.is_file() for variant in variants):
            site_marker_for(state[0]).touch()
            rebuilt += 1
    return rebuilt


async def _in_thread(func: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(func, *args)


async def sweep_once(run: Runner | None = None) -> int:
    """Clean new photos of public listings and refresh their site variants; returns their count."""
    runner = run or _in_thread
    paths = await published_photo_paths()
    todo = await asyncio.to_thread(pending, paths)
    for start in range(0, len(todo), SWEEP_BATCH):
        await runner(clean_batch, todo[start : start + SWEEP_BATCH])
    if get_settings().watermark_site_use_clean:
        for start in range(0, len(paths), SITE_VARIANT_BATCH):
            await runner(rebuild_site_variants, paths[start : start + SITE_VARIANT_BATCH])
    return len(todo)


async def sweep_forever(run: Runner | None = None) -> None:
    while True:
        try:
            done = await sweep_once(run)
            if done:
                logger.info("[watermark] site sweep cleaned %d new photos", done)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[watermark] site sweep failed; retrying later")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
