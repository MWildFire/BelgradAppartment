"""Bounded retry tick, shared with the live matcher semaphore. No new external scheduler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import or_, select

from src.db import InboundPost, InboundStatus, SourceChannel, get_session
from src.db.models import ValidationJob
from src.telegram.digest_jobs import retry_pending

logger = logging.getLogger(__name__)


async def retry_tick(on_inbound: Callable[[int], Awaitable[None]], *, limit: int = 10) -> None:
    from src.telegram.monitor import _pipeline_semaphore

    await retry_pending(limit=limit)
    now = datetime.now(UTC)
    async with get_session() as session:
        ids = list(
            (
                await session.execute(
                    select(InboundPost.id)
                    .join(SourceChannel)
                    .outerjoin(
                        ValidationJob,
                        ValidationJob.inbound_id == InboundPost.id,
                    )
                    .where(
                        InboundPost.status == InboundStatus.NEW,
                        SourceChannel.is_active.is_(True),
                        or_(
                            ValidationJob.inbound_id.is_(None),
                            ValidationJob.state.in_(["pending", "running"]),
                        ),
                        or_(
                            ValidationJob.next_retry_at.is_(None),
                            ValidationJob.next_retry_at <= now,
                        ),
                        or_(ValidationJob.lease_until.is_(None), ValidationJob.lease_until <= now),
                    )
                    .order_by(InboundPost.received_at, InboundPost.id)
                    .limit(limit)
                )
            ).scalars()
        )
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
