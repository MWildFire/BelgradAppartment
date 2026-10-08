"""Recover only automatically eligible NEW posts and account for every snapshot row."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from src.db import InboundPost, InboundStatus, get_session
from src.db.models import ValidationJob
from src.matching.validation_queue import automatic_inbound_select

logger = logging.getLogger(__name__)


async def recover_new_posts(
    on_inbound: Callable[[int], Awaitable[None]],
    *,
    notify_start: Callable[[str], Awaitable[None]],
    notify_summary: Callable[..., Awaitable[None]],
) -> dict[str, int]:
    """No manual requeue. Counts cover the initial NEW snapshot, not new arrivals.

    Errors count callback exceptions separately from the disjoint final DB states;
    a callback can commit a match before it raises, so errors are not added to totals.
    """
    async with get_session() as session:
        snapshot = list(
            (
                await session.execute(
                    select(InboundPost.id).where(
                        InboundPost.status == InboundStatus.NEW,
                    )
                )
            ).scalars()
        )
        selected = list(
            (
                await session.execute(
                    automatic_inbound_select().where(InboundPost.id.in_(snapshot)),
                )
            ).scalars()
        )
    stats = dict(
        selected=len(selected),
        matched=0,
        no_match=0,
        needs_review=0,
        pending=0,
        unresolved=0,
        errors=0,
    )
    if not snapshot:
        return stats
    started = time.monotonic()
    if selected:
        logger.info(
            "Recovery selected %d automatic NEW posts from %d",
            len(selected),
            len(snapshot),
        )
        try:
            await notify_start(
                f"🔄 Recovery: автоматически проверяем {len(selected)} пост(ов) "
                f"из {len(snapshot)} NEW в снимке очереди. Итог покажет оставшиеся нерешённые.",
            )
        except Exception:
            logger.exception("recovery start notify failed")
    for inbound_id in selected:
        try:
            await on_inbound(inbound_id)
        except Exception:
            stats["errors"] += 1
            logger.exception("recovery inbound=%s pipeline failed", inbound_id)
    async with get_session() as session:
        rows = (
            await session.execute(
                select(InboundPost.id, InboundPost.status, ValidationJob.state)
                .outerjoin(ValidationJob, ValidationJob.inbound_id == InboundPost.id)
                .where(InboundPost.id.in_(snapshot)),
            )
        ).all()
    stats["unresolved"] = len(snapshot) - len(rows)
    for _inbound_id, status, state in rows:
        if status == InboundStatus.MATCHED:
            stats["matched"] += 1
        elif status == InboundStatus.NO_MATCH:
            stats["no_match"] += 1
        elif status == InboundStatus.NEW and state == "needs_review":
            stats["needs_review"] += 1
        elif status == InboundStatus.NEW:
            stats["pending"] += 1
        else:
            stats["unresolved"] += 1
    logger.info(
        "Recovery snapshot=%d selected=%d matched=%d no_match=%d "
        "needs_review=%d pending=%d unresolved=%d errors=%d",
        len(snapshot),
        stats["selected"],
        stats["matched"],
        stats["no_match"],
        stats["needs_review"],
        stats["pending"],
        stats["unresolved"],
        stats["errors"],
    )
    try:
        await notify_summary(
            **stats,
            duration_seconds=time.monotonic() - started,
            label=f"Recovery после рестарта (NEW в снимке: {len(snapshot)})",
        )
    except Exception:
        logger.exception("recovery summary delivery failed")
    return stats
