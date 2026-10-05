"""Durable per-share ingestion. Telegram watermark means queued, not fetched."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert

from src.config import get_settings
from src.db import InboundPost, InboundStatus, SourceChannel, get_session
from src.db.models import DigestWorkItem
from src.matching.photo_manifest import build_manifest
from src.telegram.digest_parser import DigestLine


async def enqueue(
    source_id: int,
    message_id: int,
    post_date: datetime,
    lines: list[DigestLine],
) -> None:
    from src.telegram.monitor import _advance_watermark

    async with get_session() as session:
        for line in lines:
            await session.execute(
                insert(DigestWorkItem)
                .values(
                    source_id=source_id,
                    message_id=message_id,
                    share_id=line.share_id,
                    payload=asdict(line),
                    post_date=post_date,
                    state="pending",
                    attempts=0,
                    reason_code="",
                )
                .on_conflict_do_nothing(index_elements=["source_id", "message_id", "share_id"])
            )
        # In the SAME transaction, after all work items are durable.
        await _advance_watermark(session, source_id, message_id)


async def retry_pending(
    *,
    source_id: int | None = None,
    message_id: int | None = None,
    limit: int = 25,
) -> list[InboundPost]:
    now = datetime.now(UTC)
    query = (
        select(DigestWorkItem.id)
        .join(SourceChannel)
        .where(
            SourceChannel.is_active.is_(True),
            DigestWorkItem.state.in_(["pending", "running"]),
            or_(DigestWorkItem.next_retry_at.is_(None), DigestWorkItem.next_retry_at <= now),
            or_(DigestWorkItem.lease_until.is_(None), DigestWorkItem.lease_until <= now),
        )
        .order_by(DigestWorkItem.id)
        .limit(limit)
    )
    if source_id is not None:
        query = query.where(DigestWorkItem.source_id == source_id)
    if message_id is not None:
        query = query.where(DigestWorkItem.message_id == message_id)
    async with get_session() as session:
        ids = list((await session.execute(query)).scalars())
    created = []
    for item_id in ids:
        post = await _process(item_id)
        if post is not None:
            created.append(post)
    return created


async def _process(item_id: int) -> InboundPost | None:
    from src.telegram import monitor

    token, now = uuid4().hex, datetime.now(UTC)
    async with get_session() as session:
        owned = (
            await session.execute(
                update(DigestWorkItem)
                .where(
                    DigestWorkItem.id == item_id,
                    DigestWorkItem.state.in_(["pending", "running"]),
                    or_(DigestWorkItem.lease_until.is_(None), DigestWorkItem.lease_until <= now),
                    or_(
                        DigestWorkItem.next_retry_at.is_(None), DigestWorkItem.next_retry_at <= now
                    ),
                )
                .values(
                    state="running",
                    lease_token=token,
                    lease_until=now + timedelta(seconds=180),
                    attempts=DigestWorkItem.attempts + 1,
                )
                .returning(DigestWorkItem.id)
            )
        ).scalar_one_or_none()
        if owned is None:
            return None
        item = await session.get(DigestWorkItem, item_id)
        assert item is not None
        exists = (
            await session.execute(
                select(InboundPost.id).where(
                    InboundPost.source_channel_id == item.source_id,
                    InboundPost.tg_message_id == item.message_id,
                    InboundPost.digest_share_id == item.share_id,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            item.state, item.lease_token, item.lease_until = "complete", None, None
            return None

    try:
        async with asyncio.timeout(120):
            line = DigestLine(**item.payload)
            doc = await monitor.fetch_document(line.share_id, line.yonote_team)
            # Attempt-owned directories prevent an expired worker overwriting accepted media.
            target = (
                get_settings().inbound_photos_dir
                / str(item.source_id)
                / str(item.message_id)
                / f"digest-{item.id}-{token}"
            )
            paths, hashes = await monitor._download_yonote_photos(doc, target)
            if not paths:
                raise ValueError("no_photos")
            embeddings = await monitor._compute_clip_blob(paths, item.message_id)
            text = monitor._build_digest_raw_text(line, doc)
            manifest = await asyncio.to_thread(build_manifest, paths, hashes)
        async with get_session() as session:
            owned = (
                await session.execute(
                    update(DigestWorkItem)
                    .where(
                        DigestWorkItem.id == item_id,
                        DigestWorkItem.lease_token == token,
                        DigestWorkItem.lease_until > datetime.now(UTC),
                    )
                    .values(
                        state="complete",
                        lease_token=None,
                        lease_until=None,
                        reason_code="",
                        next_retry_at=None,
                    )
                    .returning(DigestWorkItem.id)
                )
            ).scalar_one_or_none()
            if owned is None:
                return None
            # UNIQUE(source, message, share) is a second guard; done + inbound commit atomically.
            post = InboundPost(
                source_channel_id=item.source_id,
                tg_message_id=item.message_id,
                digest_share_id=item.share_id,
                post_date=item.post_date,
                raw_text=text,
                photo_paths=paths,
                photo_phashes=hashes,
                photo_evidence=manifest,
                photo_embeddings=embeddings,
                status=InboundStatus.NEW,
            )
            session.add(post)
            await session.flush()
            return post
    except Exception:
        async with get_session() as session:
            await session.execute(
                update(DigestWorkItem)
                .where(
                    DigestWorkItem.id == item_id,
                    DigestWorkItem.lease_token == token,
                )
                .values(
                    state="needs_review" if item.attempts >= 5 else "pending",
                    reason_code="ingestion_failed",
                    lease_token=None,
                    lease_until=None,
                    next_retry_at=datetime.now(UTC) + timedelta(minutes=5 * item.attempts),
                )
            )
        return None
