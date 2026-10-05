"""Short transactional claims; no DB session is held during provider IO."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import case, or_, update
from sqlalchemy.dialects.sqlite import insert

from src.db import get_session
from src.db.models import ValidationJob


async def claim(inbound_id: int, *, lease_seconds: int = 120) -> str | None:
    now = datetime.now(UTC)
    token = uuid4().hex
    async with get_session() as s:
        await s.execute(
            insert(ValidationJob)
            .values(
                inbound_id=inbound_id,
                state="pending",
                reason_code="",
                attempts=0,
                outcomes=[],
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["inbound_id"])
        )
        await s.execute(
            update(ValidationJob)
            .where(
                ValidationJob.inbound_id == inbound_id,
                ValidationJob.attempts >= 5,
                ValidationJob.state.in_(["pending", "running"]),
                or_(ValidationJob.lease_until.is_(None), ValidationJob.lease_until <= now),
            )
            .values(
                state="needs_review",
                reason_code="attempt_limit",
                lease_token=None,
                lease_until=None,
                updated_at=now,
            )
        )
        claimed = (
            await s.execute(
                update(ValidationJob)
                .where(
                    ValidationJob.inbound_id == inbound_id,
                    ValidationJob.state.in_(["pending", "running"]),
                    ValidationJob.attempts < 5,
                    or_(ValidationJob.lease_until.is_(None), ValidationJob.lease_until <= now),
                    or_(ValidationJob.next_retry_at.is_(None), ValidationJob.next_retry_at <= now),
                )
                .values(
                    lease_token=token,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    state="running",
                    attempts=ValidationJob.attempts + 1,
                    updated_at=now,
                )
                .returning(ValidationJob.inbound_id)
            )
        ).scalar_one_or_none()
        return token if claimed is not None else None


async def defer(
    inbound_id: int,
    token: str,
    reason: str,
    *,
    review: bool = False,
    outcomes: list[dict[str, Any]] | None = None,
) -> None:
    now = datetime.now(UTC)
    # A circuit rejection made no provider request. Refund this claim only when
    # every candidate was skipped; a mixed pass (e.g. timeout then circuit_open)
    # must still consume its attempt, including an outer deadline or review case.
    circuit_only = (
        not review
        and reason == "circuit_open"
        and bool(outcomes)
        and all(item.get("reason_code") == "circuit_open" for item in outcomes or [])
    )
    attempts = (
        case((ValidationJob.attempts > 0, ValidationJob.attempts - 1), else_=0)
        if circuit_only
        else ValidationJob.attempts
    )
    async with get_session() as s:
        await s.execute(
            update(ValidationJob)
            .where(
                ValidationJob.inbound_id == inbound_id,
                ValidationJob.lease_token == token,
            )
            .values(
                state="needs_review"
                if review
                else case((attempts >= 5, "needs_review"), else_="pending"),
                attempts=attempts,
                reason_code=reason,
                outcomes=outcomes or [],
                lease_token=None,
                lease_until=None,
                next_retry_at=None if review else now + timedelta(minutes=5),
                updated_at=now,
            )
        )
