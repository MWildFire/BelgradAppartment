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
from typing import Any

from src.config import get_settings
from src.watermark.store import ensure_clean, is_current, resolve_stored

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 600
SWEEP_BATCH = 4

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
    """ensure_clean for each path -> (photos that had marks, photos missing on disk)."""
    changed = missing = 0
    for stored in paths:
        result = ensure_clean(stored, force=force)
        if result is None:
            missing += 1
        elif result.clean is not None:
            changed += 1
    return changed, missing


async def _in_thread(func: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(func, *args)


async def sweep_once(run: Runner | None = None) -> int:
    """Clean new photos of public listings and refresh their site variants; returns their count."""
    runner = run or _in_thread
    todo = await asyncio.to_thread(pending, await published_photo_paths())
    for start in range(0, len(todo), SWEEP_BATCH):
        await runner(clean_batch, todo[start : start + SWEEP_BATCH])
    if todo and get_settings().watermark_site_use_clean:
        from src.site.photos import generate_for_stored_paths

        await asyncio.to_thread(generate_for_stored_paths, todo)
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
