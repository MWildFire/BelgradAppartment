"""Bounded retry tick, shared with the live matcher semaphore. No new external scheduler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from src.matching.validation_queue import automatic_inbound_ids
from src.telegram.digest_jobs import retry_pending

logger = logging.getLogger(__name__)


async def retry_tick(on_inbound: Callable[[int], Awaitable[None]], *, limit: int = 10) -> None:
    from src.telegram.monitor import _pipeline_semaphore

    await retry_pending(limit=limit)
    ids = await automatic_inbound_ids(limit=limit)
    for inbound_id in ids:
        async with _pipeline_semaphore:
            try:
                await on_inbound(inbound_id)
            except Exception:
                logger.warning("[retry] pipeline failed inbound=%s", inbound_id)


async def retry_loop(on_inbound: Callable[[int], Awaitable[None]]) -> None:
    while True:
        try:
            await retry_tick(on_inbound)
        except Exception:
            logger.exception("[retry] tick failed")
        await asyncio.sleep(60)
