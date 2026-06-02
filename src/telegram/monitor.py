"""Telegram source-channel monitor.

Two modes share the same per-post processing:
- `backfill(source)`        — iterate history from the channel's watermark forward.
- `run_live(source_ids)`    — Telethon NewMessage handler for real-time arrivals.

A Telegram "post with N photos" is actually N sibling messages with the same `grouped_id`.
We accumulate messages with the same grouped_id into one `InboundPost`. Posts without photos
are skipped (per Darya's spec — text-only is out of scope for MVP).

Watermark: `SourceChannel.last_processed_message_id` is updated after each persisted post,
so re-runs are idempotent and pick up exactly where the last run left off.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.tl.custom.message import Message
from telethon.tl.types import Channel, Chat, MessageMediaPhoto, User
from telethon.utils import get_peer_id

from src.config import get_settings
from src.db import (
    InboundPost,
    InboundStatus,
    SourceChannel,
    get_session,
)
from src.integrations.yonote import (
    YoNoteDocument,
    YoNoteError,
    download_attachment,
    fetch_document,
)
from src.matching.phash import compute_phash_from_path
from src.telegram.digest_parser import DigestLine, is_digest_post, parse_digest

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- Client


def make_client() -> TelegramClient:
    """Construct a Telethon client using settings from .env. Caller must await client.start()."""
    settings = get_settings()
    if not settings.tg_api_id or not settings.tg_api_hash:
        raise RuntimeError("TG_API_ID / TG_API_HASH missing in .env — get from my.telegram.org")
    session_path = str(settings.data_dir / settings.tg_session_name)
    settings.ensure_dirs()
    return TelegramClient(session_path, settings.tg_api_id, settings.tg_api_hash)


def parse_channel_identifier(value: str) -> str:
    """Accept t.me URLs, @usernames, or bare usernames. Return bare username (no '@')."""
    s = value.strip()
    for prefix in ("https://t.me/", "http://t.me/", "tg://resolve?domain="):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s.lstrip("@").split("/")[0].split("?")[0]


# --------------------------------------------------------------------------- Source channels


async def add_source(
    client: TelegramClient, identifier: str, *, start_date: datetime | None = None
) -> SourceChannel:
    """Resolve a channel identifier and upsert a SourceChannel row."""
    username = parse_channel_identifier(identifier)
    entity = await client.get_entity(username)
    if not isinstance(entity, Channel | Chat | User):
        raise ValueError(f"Unexpected entity type for {identifier!r}: {type(entity).__name__}")

    # Use Telethon's "marked" peer id (e.g. -100xxxxxxxxxx for channels) so it matches
    # what `event.chat_id` returns inside NewMessage handlers — they must compare equal.
    tg_id = int(get_peer_id(entity))
    title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
    eff_username = getattr(entity, "username", None) or username

    settings = get_settings()
    if start_date is None:
        start_date = datetime.fromisoformat(settings.source_default_start_date).replace(
            tzinfo=timezone.utc
        )

    async with get_session() as s:
        existing = (
            await s.execute(select(SourceChannel).where(SourceChannel.tg_id == tg_id))
        ).scalar_one_or_none()
        if existing:
            existing.username = eff_username
            existing.title = title
            existing.is_active = True
            if existing.start_date is None:
                existing.start_date = start_date
            row = existing
        else:
            row = SourceChannel(
                tg_id=tg_id,
                username=eff_username,
                title=title,
                is_active=True,
                start_date=start_date,
            )
            s.add(row)
            await s.flush()
        await s.refresh(row)
        return row


# --------------------------------------------------------------------------- Backfill


@dataclass
class BackfillStats:
    processed: int = 0
    skipped_no_photo: int = 0
    posts_inserted: int = 0


async def backfill(
    client: TelegramClient,
    source: SourceChannel,
    *,
    limit: int | None = None,
    on_inbound: "OnInboundCallable | None" = None,
) -> BackfillStats:
    """Backfill historical posts from the channel's watermark forward.

    Idempotent: replays from `last_processed_message_id + 1` (or `start_date` if first run).
    Updates the watermark after each inserted post.
    """
    stats = BackfillStats()
    entity = await client.get_entity(source.username or source.tg_id)

    kwargs: dict[str, object] = {"reverse": True}  # chronological order
    if source.last_processed_message_id:
        kwargs["min_id"] = source.last_processed_message_id
    elif source.start_date:
        kwargs["offset_date"] = source.start_date

    async for group in _iter_albums(client.iter_messages(entity, **kwargs), limit=limit):
        # Digest posts contain YoNote links and do not need their banner photo, so the
        # photo gate only applies to ordinary reposts.
        if not _has_photo(group) and not is_digest_post(group[0]):
            stats.skipped_no_photo += 1
            continue
        posts = await _persist_inbound(source.id, group)
        stats.processed += 1
        for post in posts:
            stats.posts_inserted += 1
            if on_inbound is not None:
                try:
                    await on_inbound(post.id)
                except Exception:
                    logger.exception("on_inbound callback failed for post %s", post.id)
    return stats


# --------------------------------------------------------------------------- Live


OnInboundCallable = "callable"  # type alias hint — runtime: any coroutine taking int


def register_live_handler(
    client: TelegramClient,
    source_channels: Iterable[SourceChannel],
    on_inbound: OnInboundCallable | None = None,
) -> None:
    """Wire a NewMessage handler that processes album-grouped photos into InboundPost rows.

    `on_inbound(inbound_id)` is called for every newly-persisted post. In production this is
    `process_new_inbound(...)` from src.jobs.pipeline — it scrapes (throttled) and delivers.
    """
    sources_by_tg = {s.tg_id: s for s in source_channels}
    chat_filter: list[int] = list(sources_by_tg.keys())
    if not chat_filter:
        logger.warning("No active source channels — live handler will receive nothing")
        return

    # Album buffer: grouped_id -> list[Message] flushed by short delay
    album_buffers: dict[int, list[Message]] = {}
    flush_tasks: dict[int, asyncio.Task[None]] = {}
    ALBUM_FLUSH_DELAY = 2.5  # seconds — wait for sibling album items to arrive

    async def _flush(gid: int, source_id: int) -> None:
        await asyncio.sleep(ALBUM_FLUSH_DELAY)
        msgs = album_buffers.pop(gid, [])
        flush_tasks.pop(gid, None)
        if not msgs:
            return
        if not _has_photo(msgs) and not is_digest_post(msgs[0]):
            return
        posts = await _persist_inbound(source_id, sorted(msgs, key=lambda m: m.id))
        for post in posts:
            if on_inbound is None:
                continue
            try:
                await on_inbound(post.id)
            except Exception:
                logger.exception("on_inbound callback failed for live post %s", post.id)

    @client.on(events.NewMessage(chats=chat_filter))
    async def _handler(event: events.NewMessage.Event) -> None:
        msg: Message = event.message
        source = sources_by_tg.get(int(event.chat_id))
        if source is None:
            return
        gid = msg.grouped_id
        if gid is not None:
            album_buffers.setdefault(gid, []).append(msg)
            if gid not in flush_tasks:
                flush_tasks[gid] = asyncio.create_task(_flush(gid, source.id))
            return
        # standalone message
        if not _has_photo([msg]) and not is_digest_post(msg):
            return
        posts = await _persist_inbound(source.id, [msg])
        for post in posts:
            if on_inbound is None:
                continue
            try:
                await on_inbound(post.id)
            except Exception:
                logger.exception("on_inbound callback failed for live post %s", post.id)

    logger.info("Live handler registered for %d source(s)", len(chat_filter))


# Re-export for tests / callers that import from monitor directly.
__all__ = [
    "BackfillStats",
    "add_source",
    "backfill",
    "make_client",
    "parse_channel_identifier",
    "register_live_handler",
]


# --------------------------------------------------------------------------- Internals


async def _iter_albums(
    cursor: AsyncIterator[Message], *, limit: int | None
) -> AsyncIterator[list[Message]]:
    """Group iter_messages output into album batches (same grouped_id, sequential).

    Telethon yields one Message per item; an album of N photos is N messages with the same
    grouped_id. We accumulate same-grouped_id messages and flush on grouped_id boundary.
    """
    buffer: list[Message] = []
    buffer_gid: int | None = None
    flushed = 0

    async for msg in cursor:
        gid = msg.grouped_id
        same = gid is not None and gid == buffer_gid
        if same:
            buffer.append(msg)
            continue
        if buffer:
            yield buffer
            flushed += 1
            if limit is not None and flushed >= limit:
                return
        buffer = [msg]
        buffer_gid = gid
    if buffer:
        yield buffer


def _has_photo(msgs: list[Message]) -> bool:
    for m in msgs:
        if m.photo is not None:
            return True
        media = getattr(m, "media", None)
        if isinstance(media, MessageMediaPhoto):
            return True
    return False


async def _persist_inbound(source_id: int, msgs: list[Message]) -> list[InboundPost]:
    """Persist InboundPost(s) for a Telegram album.

    Two code paths:
    - Digest posts (serbia4rent-style list of YoNote share links) become one InboundPost per
      apartment row — photos are pulled from YoNote, not from the Telegram message itself.
    - Everything else is one InboundPost; photos come from the album.

    Returns the list of newly-created posts (may be empty when nothing was persistable).
    """
    if not msgs:
        return []
    msgs = sorted(msgs, key=lambda m: m.id)
    leader = msgs[0]

    if is_digest_post(leader):
        return await _persist_digest_inbound(source_id, leader)

    return await _persist_regular_inbound(source_id, msgs)


async def _persist_regular_inbound(source_id: int, msgs: list[Message]) -> list[InboundPost]:
    leader = msgs[0]
    post_date = leader.date or datetime.now(timezone.utc)
    if post_date.tzinfo is None:
        post_date = post_date.replace(tzinfo=timezone.utc)

    settings = get_settings()
    target_dir = settings.inbound_photos_dir / str(source_id) / str(leader.id)
    target_dir.mkdir(parents=True, exist_ok=True)

    photo_paths: list[str] = []
    photo_hashes: list[str] = []
    for i, m in enumerate(msgs):
        if m.photo is None and not isinstance(getattr(m, "media", None), MessageMediaPhoto):
            continue
        path = target_dir / f"{i:02d}.jpg"
        try:
            saved = await m.download_media(file=str(path))
        except Exception:
            logger.exception("download_media failed for msg %s", m.id)
            continue
        if not saved:
            continue
        try:
            phash = compute_phash_from_path(Path(saved))
        except Exception:
            logger.exception("phash failed for %s", saved)
            continue
        photo_paths.append(str(saved))
        photo_hashes.append(phash)

    if not photo_paths:
        return []

    # Concatenate text from the album (caption is usually only on first message).
    raw_text = " ".join(filter(None, ((m.message or "").strip() for m in msgs))).strip()

    embeddings_blob = await _compute_clip_blob(photo_paths, leader.id)

    async with get_session() as s:
        existing = (
            await s.execute(
                select(InboundPost).where(
                    InboundPost.source_channel_id == source_id,
                    InboundPost.tg_message_id == leader.id,
                    InboundPost.digest_share_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await _advance_watermark(s, source_id, msgs[-1].id)
            return []

        post = InboundPost(
            source_channel_id=source_id,
            tg_message_id=leader.id,
            post_date=post_date,
            raw_text=raw_text,
            photo_paths=photo_paths,
            photo_phashes=photo_hashes,
            photo_embeddings=embeddings_blob,
            status=InboundStatus.NEW,
        )
        s.add(post)
        await _advance_watermark(s, source_id, msgs[-1].id)
        await s.flush()
        await s.refresh(post)
        return [post]


async def _persist_digest_inbound(source_id: int, leader: Message) -> list[InboundPost]:
    """One Telegram message → N InboundPosts (one per YoNote share inside the digest).

    For each share: fetch metadata + photos from YoNote, hash them, persist a fresh InboundPost
    identified by `digest_share_id`. Skips lines whose YoNote fetch fails (logged) — partial
    results are better than dropping the whole digest.

    Watermark is only advanced after *all* lines have been processed (or the digest is empty/
    fully duplicated). If we crash mid-digest (OOM, network) the watermark stays behind, so a
    restart re-enters the same `leader.id` and the per-line idempotency check skips already-
    persisted shares while processing the missing ones.
    """
    lines = parse_digest(leader)
    if not lines:
        logger.warning("[monitor] digest msg %s had no parsable lines", leader.id)
        # Still advance the watermark so we don't reprocess.
        async with get_session() as s:
            await _advance_watermark(s, source_id, leader.id)
        return []

    post_date = leader.date or datetime.now(timezone.utc)
    if post_date.tzinfo is None:
        post_date = post_date.replace(tzinfo=timezone.utc)

    settings = get_settings()
    created: list[InboundPost] = []

    for idx, line in enumerate(lines):
        # One sub-directory per share so LLM validator and downstream code can locate the
        # apartment's photos.
        slug = line.share_id.split("-")[0]
        target_dir = settings.inbound_photos_dir / str(source_id) / str(leader.id) / f"digest-{slug}"

        try:
            yonote_doc = await fetch_document(line.share_id, line.yonote_team)
        except YoNoteError:
            logger.exception("[monitor] yonote fetch failed for share %s", line.share_id)
            continue
        except Exception:
            logger.exception("[monitor] unexpected error fetching yonote share %s", line.share_id)
            continue

        photo_paths, photo_hashes = await _download_yonote_photos(yonote_doc, target_dir)
        if not photo_paths:
            logger.warning(
                "[monitor] yonote share %s had no downloadable photos — skipping",
                line.share_id,
            )
            continue

        embeddings_blob = await _compute_clip_blob(photo_paths, leader.id)
        raw_text = _build_digest_raw_text(line, yonote_doc)

        async with get_session() as s:
            existing = (
                await s.execute(
                    select(InboundPost).where(
                        InboundPost.source_channel_id == source_id,
                        InboundPost.tg_message_id == leader.id,
                        InboundPost.digest_share_id == line.share_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue

            post = InboundPost(
                source_channel_id=source_id,
                tg_message_id=leader.id,
                digest_share_id=line.share_id,
                post_date=post_date,
                raw_text=raw_text,
                photo_paths=photo_paths,
                photo_phashes=photo_hashes,
                photo_embeddings=embeddings_blob,
                status=InboundStatus.NEW,
            )
            s.add(post)
            await s.flush()
            await s.refresh(post)
            created.append(post)
            logger.info(
                "[monitor] digest msg=%s line=%d share=%s → inbound=%s (%d photos)",
                leader.id, idx, line.share_id[:8], post.id, len(photo_paths),
            )

    # Only advance the watermark once the whole digest is done — if a previous run crashed
    # mid-digest, this lets us re-enter the same leader.id and pick up the missed shares
    # (the per-line `existing` check makes the re-entry idempotent for shares we already saved).
    async with get_session() as s:
        await _advance_watermark(s, source_id, leader.id)

    return created


async def _download_yonote_photos(
    doc: YoNoteDocument, target_dir: Path
) -> tuple[list[str], list[str]]:
    target_dir.mkdir(parents=True, exist_ok=True)
    photo_paths: list[str] = []
    photo_hashes: list[str] = []
    for i, att in enumerate(doc.attachments):
        ext = ".png" if att.content_type.endswith("png") else ".jpg"
        dest = target_dir / f"{i:02d}{ext}"
        try:
            await download_attachment(doc, att, dest)
        except YoNoteError:
            logger.exception("[monitor] yonote attachment %s failed", att.id)
            continue
        except Exception:
            logger.exception("[monitor] unexpected error downloading %s", att.id)
            continue
        try:
            phash = compute_phash_from_path(dest)
        except Exception:
            logger.exception("[monitor] phash failed for %s", dest)
            continue
        photo_paths.append(str(dest))
        photo_hashes.append(phash)
    return photo_paths, photo_hashes


def _build_digest_raw_text(line: DigestLine, doc: YoNoteDocument) -> str:
    """Compose raw_text the rest of the pipeline (parser/template) can consume.

    Order matters: the digest line goes first (price/district extractors hit it first), the
    YoNote body provides the street + condition details, the YoNote URL is appended last so a
    human glancing at delivered posts can verify.
    """
    parts: list[str] = [line.raw_line]
    if doc.title:
        parts.append(doc.title)
    if doc.text:
        parts.append(doc.text)
    parts.append(f"Источник: {line.yonote_url}")
    return "\n\n".join(p for p in parts if p)


async def _compute_clip_blob(photo_paths: list[str], leader_id: int) -> bytes | None:
    """CLIP ViT-B/32 embeddings packed as float16 BLOB — same format as listings."""
    try:
        from src.matching.clip_embed import compute_embeddings_batch, pack_embeddings

        vecs = await asyncio.to_thread(
            compute_embeddings_batch, [Path(p) for p in photo_paths], batch_size=4
        )
        return pack_embeddings(vecs)
    except Exception:
        logger.exception("CLIP embedding failed for inbound msg %s", leader_id)
        return None


async def _advance_watermark(s, source_id: int, last_msg_id: int) -> None:
    chan = await s.get(SourceChannel, source_id)
    if chan is not None:
        chan.last_processed_message_id = max(chan.last_processed_message_id or 0, last_msg_id)
        chan.last_processed_at = datetime.now(timezone.utc)
