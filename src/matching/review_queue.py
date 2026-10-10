"""Local operator inspection/requeue. These operations never call providers or delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, exists, or_, select, update

from src.db import InboundPost, InboundStatus, Match, MatchStatus, get_session
from src.db.models import DigestWorkItem, MatchDelivery, ValidationJob

REVIEW_DECISIONS = {"NO_MATCH": InboundStatus.NO_MATCH, "DUPLICATE": InboundStatus.DUPLICATE}
REASON_MAX_LENGTH = 256


class ReviewError(Exception):
    """Operator decision refused; nothing was written."""


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
    """Operator retry for an automatic case; a manual decision stays terminal."""
    now = datetime.now(UTC)
    async with get_session() as session:
        owned = (
            await session.execute(
                update(ValidationJob)
                .where(
                    ValidationJob.inbound_id == inbound_id,
                    or_(ValidationJob.lease_until.is_(None), ValidationJob.lease_until <= now),
                    or_(
                        ValidationJob.reason_code.is_(None),
                        ValidationJob.reason_code.not_like("manual_%"),
                    ),
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


def _clean_reason(reason: str) -> str:
    cleaned = reason.strip()
    if not cleaned:
        raise ReviewError("укажите причину решения")
    if len(cleaned) > REASON_MAX_LENGTH:
        raise ReviewError(f"причина длиннее {REASON_MAX_LENGTH} символов")
    return cleaned


async def close_review(inbound_id: int, decision: str, reason: str) -> None:
    """Terminal manual decision for a needs_review case; never sends or calls providers.

    One transaction: the job leaves needs_review only without a live lease and without
    Match rows, and the inbound leaves NEW in the same step. Any refusal rolls both back.
    """
    if decision not in REVIEW_DECISIONS:
        raise ReviewError(f"решение должно быть одним из: {', '.join(REVIEW_DECISIONS)}")
    reason = _clean_reason(reason)
    now = datetime.now(UTC)
    async with get_session() as session:
        closed = (
            await session.execute(
                update(ValidationJob)
                .where(
                    ValidationJob.inbound_id == inbound_id,
                    ValidationJob.state == "needs_review",
                    or_(ValidationJob.lease_until.is_(None), ValidationJob.lease_until <= now),
                    ~exists(select(Match.id).where(Match.inbound_post_id == inbound_id)),
                )
                .values(
                    state="complete",
                    reason_code=f"manual_{decision.lower()}",
                    lease_token=None,
                    lease_until=None,
                    next_retry_at=None,
                    updated_at=now,
                )
                .returning(ValidationJob.inbound_id)
            )
        ).scalar_one_or_none()
        if closed is None:
            raise ReviewError(f"inbound {inbound_id}: нет кейса needs_review без аренды и матчей")
        moved = (
            await session.execute(
                update(InboundPost)
                .where(InboundPost.id == inbound_id, InboundPost.status == InboundStatus.NEW)
                .values(status=REVIEW_DECISIONS[decision])
                .returning(InboundPost.id)
            )
        ).scalar_one_or_none()
        if moved is None:
            raise ReviewError(f"inbound {inbound_id}: статус уже не NEW")
        job = await session.get(ValidationJob, inbound_id)
        journal = {
            "decision": decision,
            "reason": reason,
            "decided_at": now.isoformat(),
            "source": "cli",
        }
        job.outcomes = [*(job.outcomes or []), journal]


async def reject_match(match_id: int, reason: str) -> str:
    """Reject a PENDING match that was never sent; PUBLISHED matches stay with the admin tool.

    The status change is one conditional statement that applies only while no delivery record
    exists. The recipient claim refuses a REJECTED match, so the two cannot interleave.
    """
    reason = _clean_reason(reason)
    async with get_session() as session:
        match = await session.get(Match, match_id)
        if match is None:
            raise ReviewError(f"match {match_id} не найден")
        if match.status == MatchStatus.REJECTED:
            return "already_rejected"
        if match.status != MatchStatus.PENDING:
            raise ReviewError(
                f"match {match_id}: статус {match.status.value}; отклоняйте через админку"
            )
        rejected = (
            await session.execute(
                update(Match)
                .where(
                    Match.id == match_id,
                    Match.status == MatchStatus.PENDING,
                    ~exists(select(MatchDelivery.id).where(MatchDelivery.match_id == match_id)),
                )
                .values(status=MatchStatus.REJECTED, rejection_reason=reason)
                .returning(Match.id)
            )
        ).scalar_one_or_none()
        if rejected is None:
            raise ReviewError(f"match {match_id}: есть записи доставки или статус изменился")
        return "rejected"


async def reset_for_rematch(*, keep_published: bool) -> None:
    """Operator reset before a full rematch; operator decisions are not erased.

    With keep_published, published matches, rejected matches and manual review decisions survive.
    Other inbound posts return to NEW, and their non-published, non-rejected matches are removed.
    Without keep_published the caller has already confirmed an explicit full wipe.
    """
    async with get_session() as session:
        if keep_published:
            await session.execute(
                delete(Match).where(
                    Match.status.notin_([MatchStatus.PUBLISHED, MatchStatus.REJECTED])
                )
            )
            kept = set(
                (
                    await session.execute(
                        select(InboundPost.id)
                        .join(Match, Match.inbound_post_id == InboundPost.id)
                        .where(Match.status == MatchStatus.PUBLISHED)
                    )
                ).scalars()
            )
            kept |= set(
                (
                    await session.execute(
                        select(ValidationJob.inbound_id).where(
                            ValidationJob.reason_code.like("manual_%")
                        )
                    )
                ).scalars()
            )
            reset = update(InboundPost).values(status=InboundStatus.NEW)
            if kept:
                reset = reset.where(~InboundPost.id.in_(kept))
            await session.execute(reset)
        else:
            await session.execute(delete(Match))
            await session.execute(update(InboundPost).values(status=InboundStatus.NEW))
        await session.execute(
            delete(ValidationJob).where(
                ValidationJob.inbound_id.in_(
                    select(InboundPost.id).where(InboundPost.status == InboundStatus.NEW)
                )
            )
        )
