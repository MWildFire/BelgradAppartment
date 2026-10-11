"""Local operator inspection/requeue. These operations never call providers or delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, exists, or_, select, update

from src.db import InboundPost, InboundStatus, Match, MatchStatus, get_session
from src.db.models import DigestWorkItem, MatchDelivery, PublishedPost, SiteListing, ValidationJob

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
    """Reject a PENDING match that no recipient has started to receive.

    The status change is one conditional statement that applies only while no recipient is in
    sending, sent or unknown state and no PublishedPost row exists. Queued rows stay pending and
    are never claimed for a REJECTED match. PUBLISHED matches stay with the admin tool.
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
                    ~exists(
                        select(MatchDelivery.id).where(
                            MatchDelivery.match_id == match_id,
                            MatchDelivery.status.in_(["sending", "sent", "unknown"]),
                        )
                    ),
                    ~exists(select(PublishedPost.id).where(PublishedPost.match_id == match_id)),
                )
                .values(status=MatchStatus.REJECTED, rejection_reason=reason)
                .returning(Match.id)
            )
        ).scalar_one_or_none()
        if rejected is None:
            raise ReviewError(
                f"match {match_id}: есть записи доставки или публикация, либо статус изменился"
            )
        return "rejected"


def _decided_match_clause():
    """Matches that no reset and no test wipe may delete.

    Covers operator and recipient decisions: a rejected or published match, any match with a
    delivery row (queued, in flight or sent), a PublishedPost, and a site overlay. Deleting any
    of them lets the same pair be matched and delivered again.
    """
    return or_(
        Match.status.in_([MatchStatus.PUBLISHED, MatchStatus.REJECTED]),
        Match.id.in_(select(MatchDelivery.match_id)),
        Match.id.in_(select(PublishedPost.match_id)),
        Match.id.in_(select(SiteListing.match_id)),
    )


async def reset_for_rematch() -> None:
    """Operator reset before a full rematch: only undecided matches are dropped.

    Decided matches (see _decided_match_clause) and every manual review decision stay unchanged
    together with their inbound posts. The other matches are removed and their inbound posts
    return to NEW. There is no full wipe: a wipe would send already-decided pairs again.
    """
    async with get_session() as session:
        # The write comes first, and nothing commits before the function returns. SQLite then
        # holds its write lock for the whole transaction, so the reads below see the same decided
        # state as the writes. Reads taken before any lock could miss a decision committed in
        # between; that decision would be reset to NEW and lose its manual job. This relies on
        # SQLite's locking: another database needs an explicit lock here.
        await session.execute(delete(Match).where(~_decided_match_clause()))
        kept = set(
            (
                await session.execute(select(Match.inbound_post_id).where(_decided_match_clause()))
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
        jobs = delete(ValidationJob).where(
            ValidationJob.inbound_id.in_(
                select(InboundPost.id).where(InboundPost.status == InboundStatus.NEW)
            )
        )
        if kept:
            jobs = jobs.where(~ValidationJob.inbound_id.in_(kept))
        await session.execute(jobs)


async def decided_inbound_ids(inbound_ids: list[int]) -> set[int]:
    """Posts that an operator tool must not wipe: manual review decisions and decided matches."""
    if not inbound_ids:
        return set()
    async with get_session() as session:
        manual = (
            await session.execute(
                select(ValidationJob.inbound_id).where(
                    ValidationJob.inbound_id.in_(inbound_ids),
                    ValidationJob.reason_code.like("manual_%"),
                )
            )
        ).scalars().all()
        decided = (
            await session.execute(
                select(Match.inbound_post_id).where(
                    Match.inbound_post_id.in_(inbound_ids), _decided_match_clause()
                )
            )
        ).scalars().all()
    return set(manual) | set(decided)


async def reset_undecided_for_test(inbound_ids: list[int]) -> int:
    """Drop the undecided matches of these posts and return the posts to NEW for a test run.

    Each statement re-checks the decided rules, so a decision recorded after the caller's
    decided_inbound_ids check is kept. Returns the number of matches dropped.
    """
    if not inbound_ids:
        return 0
    undecided = select(InboundPost.id).where(
        InboundPost.id.in_(inbound_ids),
        ~InboundPost.id.in_(select(Match.inbound_post_id).where(_decided_match_clause())),
        ~InboundPost.id.in_(
            select(ValidationJob.inbound_id).where(ValidationJob.reason_code.like("manual_%"))
        ),
    )
    async with get_session() as session:
        dropped = await session.execute(
            delete(Match).where(Match.inbound_post_id.in_(undecided), ~_decided_match_clause())
        )
        await session.execute(delete(ValidationJob).where(ValidationJob.inbound_id.in_(undecided)))
        await session.execute(
            update(InboundPost).where(InboundPost.id.in_(undecided)).values(status=InboundStatus.NEW)
        )
    return dropped.rowcount
