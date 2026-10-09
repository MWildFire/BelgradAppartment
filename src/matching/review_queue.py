"""Local operator inspection/requeue. These operations never call providers or delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, or_, select, update

from src.db import InboundPost, InboundStatus, Match, get_session
from src.db.models import DigestWorkItem, ValidationJob


async def pending_report(limit: int = 50) -> dict[str, list[dict[str, Any]]]:
    async with get_session() as session:
        validation = list(
            (
                await session.execute(
                    select(ValidationJob)
                    .where(ValidationJob.state != "complete")
                    .order_by(ValidationJob.updated_at)
                    .limit(limit)
                )
            ).scalars()
        )
        digest = list(
            (
                await session.execute(
                    select(DigestWorkItem)
                    .where(DigestWorkItem.state != "complete")
                    .order_by(DigestWorkItem.id)
                    .limit(limit)
                )
            ).scalars()
        )
        return {
            "validation": [
                {
                    "inbound_id": row.inbound_id,
                    "state": row.state,
                    "reason": row.reason_code,
                    "attempts": row.attempts,
                    "next_retry_at": row.next_retry_at.isoformat() if row.next_retry_at else None,
                }
                for row in validation
            ],
            "digest": [
                {
                    "work_id": row.id,
                    "state": row.state,
                    "reason": row.reason_code,
                    "attempts": row.attempts,
                    "next_retry_at": row.next_retry_at.isoformat() if row.next_retry_at else None,
                }
                for row in digest
            ],
        }


async def requeue_validation(inbound_id: int) -> bool:
    now = datetime.now(UTC)
    async with get_session() as session:
        owned = (
            await session.execute(
                update(ValidationJob)
                .where(
                    ValidationJob.inbound_id == inbound_id,
                    or_(ValidationJob.lease_until.is_(None), ValidationJob.lease_until <= now),
                    ~exists(select(Match.id).where(Match.inbound_post_id == inbound_id)),
                )
                .values(
                    state="pending",
                    reason_code="operator_retry",
                    attempts=0,
                    lease_token=None,
                    lease_until=None,
                    next_retry_at=None,
                    updated_at=now,
                )
                .returning(ValidationJob.inbound_id)
            )
        ).scalar_one_or_none()
        if owned is None:
            return False
        await session.execute(
            update(InboundPost).where(InboundPost.id == inbound_id).values(status=InboundStatus.NEW)
        )
        return True
