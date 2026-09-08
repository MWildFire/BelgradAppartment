"""Recipient claims and confirmed outcomes; network ambiguity never becomes an automatic retry."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aiogram.exceptions import TelegramNetworkError
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.db import get_session
from src.db.models import MatchDelivery

CLAIM_TIMEOUT = timedelta(minutes=15)


def ambiguous_send_error(error: Exception) -> bool:
    return isinstance(error, (TimeoutError, ConnectionError, OSError, TelegramNetworkError))


async def prepare_recipients(
    match_id: int, recipients: list[int], *, force: bool = False, payload: dict | None = None,
) -> None:
    now = datetime.now(UTC)
    async with get_session() as session:
        existing = (await session.execute(select(MatchDelivery.id).where(
            MatchDelivery.match_id == match_id
        ).limit(1))).scalar_one_or_none()
        if force:
            unknown = (await session.execute(select(MatchDelivery.id).where(
                MatchDelivery.match_id == match_id, MatchDelivery.status == "unknown"
            ).limit(1))).scalar_one_or_none()
            if unknown is not None:
                raise ValueError("reconcile unknown outcomes before force_resend")
            sending = (await session.execute(select(MatchDelivery.id).where(
                MatchDelivery.match_id == match_id, MatchDelivery.status == "sending"
            ).limit(1))).scalar_one_or_none()
            if sending is not None:
                raise ValueError("delivery already in flight")
        if existing is None or force:
            await session.execute(sqlite_insert(MatchDelivery).values([
                {"match_id": match_id, "recipient_id": recipient, "status": "pending",
                 "message_ids": [], "payload": payload, "attempts": 0,
                 "created_at": now, "updated_at": now}
                for recipient in dict.fromkeys(recipients)
            ]).on_conflict_do_nothing(index_elements=["match_id", "recipient_id"]))
        if force:
            # Explicit manual resend can replay known sent/historical delivery. UNKNOWN needs
            # separate evidence reconciliation; active claims belong to another process.
            await session.execute(update(MatchDelivery).where(
                MatchDelivery.match_id == match_id,
                MatchDelivery.recipient_id.in_(recipients),
                MatchDelivery.status.in_(["sent", "failed"]),
            ).values(status="pending", claim_token=None, error_code=None, updated_at=now, payload=payload))
        await session.execute(update(MatchDelivery).where(
            MatchDelivery.match_id == match_id, MatchDelivery.status == "sending",
            MatchDelivery.claimed_at < now - CLAIM_TIMEOUT,
        ).values(status="unknown", error_code="interrupted_send", updated_at=now))


async def claim_recipient(match_id: int, recipient_id: int) -> str | None:
    token = uuid4().hex
    now = datetime.now(UTC)
    async with get_session() as session:
        claimed = (await session.execute(update(MatchDelivery).where(
            MatchDelivery.match_id == match_id, MatchDelivery.recipient_id == recipient_id,
            MatchDelivery.status.in_(["pending", "failed"]),
        ).values(status="sending", claim_token=token, claimed_at=now,
                 attempts=MatchDelivery.attempts + 1, updated_at=now)
          .returning(MatchDelivery.id))).scalar_one_or_none()
    return token if claimed is not None else None


async def finish_recipient(
    match_id: int, recipient_id: int, token: str, *,
    message_ids: list[int] | None = None, error: Exception | None = None,
) -> None:
    now = datetime.now(UTC)
    if message_ids:
        status = "sent"
    elif error is None or ambiguous_send_error(error):
        status = "unknown"
    else:
        status = "failed"
    async with get_session() as session:
        await session.execute(update(MatchDelivery).where(
            MatchDelivery.match_id == match_id, MatchDelivery.recipient_id == recipient_id,
            MatchDelivery.status == "sending", MatchDelivery.claim_token == token,
        ).values(status=status, message_ids=message_ids or [],
                 delivered_at=now if status == "sent" else None,
                 error_code=type(error).__name__[:64] if error else None,
                 claim_token=None, updated_at=now))


async def reconcile_match_delivery(
    match_id: int, recipient_id: int, *,
    delivered_message_ids: list[int] | None = None, confirmed_not_sent: bool = False,
) -> bool:
    """Operator-only reconciliation after checking provider/recipient evidence.

    Confirmed IDs record sent. Explicit confirmed-not-sent releases only UNKNOWN for retry.
    This helper is never called by automatic jobs and does not itself send anything.
    """
    if bool(delivered_message_ids) == confirmed_not_sent:
        raise ValueError("provide confirmed message IDs OR confirmed_not_sent")
    if delivered_message_ids and any(message_id <= 0 for message_id in delivered_message_ids):
        raise ValueError("message IDs must be positive")
    now = datetime.now(UTC)
    async with get_session() as session:
        result = await session.execute(update(MatchDelivery).where(
            MatchDelivery.match_id == match_id, MatchDelivery.recipient_id == recipient_id,
            MatchDelivery.status == "unknown",
        ).values(status="sent" if delivered_message_ids else "failed",
                 message_ids=delivered_message_ids or [], claim_token=None,
                 error_code="operator_reconciled", updated_at=now,
                 delivered_at=now if delivered_message_ids else None))
        return bool(result.rowcount)


async def claimed_payload(match_id: int, recipient_id: int, token: str) -> dict:
    """Read the original fanout snapshot after winning the claim, including concurrent runs."""
    async with get_session() as session:
        payload = (await session.execute(select(MatchDelivery.payload).where(
            MatchDelivery.match_id == match_id, MatchDelivery.recipient_id == recipient_id,
            MatchDelivery.status == "sending", MatchDelivery.claim_token == token,
        ))).scalar_one_or_none()
    if not isinstance(payload, dict) or not isinstance(payload.get("caption"), str):
        raise ValueError("delivery payload missing; reconcile before retry")
    paths = payload.get("photo_paths")
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 10
            or not all(isinstance(p, str) and p for p in paths)):
        raise ValueError("delivery media payload invalid")
    return payload
