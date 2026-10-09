"""Shared read-only selection for startup recovery, retry and the refresh gate."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.sql import Select

from src.db import InboundPost, InboundStatus, SourceChannel, get_session
from src.db.models import ValidationJob


def automatic_inbound_select(now: datetime | None = None) -> Select:
    """Use the existing retry rules; claim() remains the transactional authority.

    Exhausted pending attempts still enter claim(), which moves them to review
    without provider IO. Manual, future and leased work never enters recovery.
    """
    now = now or datetime.now(UTC)
    return (
        select(InboundPost.id)
        .join(SourceChannel)
        .outerjoin(ValidationJob, ValidationJob.inbound_id == InboundPost.id)
        .where(
            InboundPost.status == InboundStatus.NEW,
            SourceChannel.is_active.is_(True),
            or_(
                ValidationJob.inbound_id.is_(None),
                ValidationJob.state.in_(["pending", "running"]),
            ),
            or_(ValidationJob.next_retry_at.is_(None), ValidationJob.next_retry_at <= now),
            or_(ValidationJob.lease_until.is_(None), ValidationJob.lease_until <= now),
        )
        .order_by(InboundPost.received_at, InboundPost.id)
    )


async def automatic_inbound_ids(*, limit: int | None = None) -> list[int]:
    statement = automatic_inbound_select()
    if limit is not None:
        statement = statement.limit(limit)
    async with get_session() as session:
        return list((await session.execute(statement)).scalars())


async def may_refresh(inbound_id: int) -> bool:
    """Avoid source IO for work claim() cannot process, including exhausted jobs."""
    statement = automatic_inbound_select().where(
        InboundPost.id == inbound_id,
        or_(ValidationJob.inbound_id.is_(None), ValidationJob.attempts < 5),
    )
    async with get_session() as session:
        return (await session.execute(statement)).scalar_one_or_none() is not None


async def unresolved_counts(inbound_ids: list[int] | None = None) -> dict[str, int]:
    """Count manual and other NEW work, including inactive/future/leased rows."""
    statement = (
        select(ValidationJob.state)
        .select_from(InboundPost)
        .outerjoin(ValidationJob, ValidationJob.inbound_id == InboundPost.id)
        .where(InboundPost.status == InboundStatus.NEW)
    )
    if inbound_ids is not None:
        statement = statement.where(InboundPost.id.in_(inbound_ids))
    async with get_session() as session:
        states = list((await session.execute(statement)).scalars())
    manual = states.count("needs_review")
    return {"needs_review": manual, "pending": len(states) - manual}
