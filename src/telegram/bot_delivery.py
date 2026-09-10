"""Send matched candidates and run summaries via the Telegram Bot API.

Recommended over the userbot path because:
- Recipients see a dedicated @RentFinderBot, not the user's personal account.
- Bot can message multiple chat_ids in one go (everyone in TG_BOT_RECIPIENTS).
- No "must be a contact" requirement — recipients only need to /start the bot once.

The userbot path remains as a fallback (TG_DELIVERY_MODE=userbot) for testing or if the bot
gets restricted. Both paths share the same `send_match_to_saved` entry point in `delivery.py`,
which dispatches based on settings.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InputMediaPhoto
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import selectinload

from src.admin.publish import auto_publish_site_listing
from src.config import get_settings
from src.db import (
    InboundPost,
    Listing,
    Match,
    MatchStatus,
    PublishedPost,
    get_session,
    resolve_post_send_status,
)
from src.db.models import MatchDelivery
from src.post import render_post
from src.telegram.delivery_ledger import (
    claim_recipient,
    claimed_payload,
    finish_recipient,
    prepare_recipients,
)

logger = logging.getLogger(__name__)

MAX_ALBUM = 10
TELEGRAM_CAPTION_LIMIT = 1024
BOT_PHOTO_LIMIT_BYTES = 10 * 1024 * 1024


class BotDeliveryError(RuntimeError):
    """Raised when a match cannot be delivered through the Bot API."""


class BotPhotoTooLargeError(BotDeliveryError):
    """Known local rejection: this photo cannot be uploaded through the Bot API."""


def _preflight_photo_sizes(photo_paths: list[Path]) -> None:
    """Validate the selected originals without resizing/reordering or reading their bytes.

    Bot API sendPhoto limits each uploaded photo to 10 MB, also for InputMediaPhoto albums:
    https://core.telegram.org/bots/api#sendphoto. This limit does not apply to Telethon.
    """
    oversized = []
    for index, path in enumerate(photo_paths, start=1):
        try:
            size = path.stat().st_size
        except OSError as error:
            raise BotDeliveryError(
                f"Bot API preflight cannot inspect photo {index}; no upload attempted"
            ) from error
        if size > BOT_PHOTO_LIMIT_BYTES:
            oversized.append(f"photo {index} ({size} bytes)")
    if oversized:
        raise BotPhotoTooLargeError(
            f"Bot API preflight: {', '.join(oversized)} exceeds the per-photo "
            f"10 MB limit ({BOT_PHOTO_LIMIT_BYTES} bytes); no upload attempted"
        )


def _recipients() -> list[int]:
    """Parse TG_BOT_RECIPIENTS — comma-separated chat_ids."""
    raw = get_settings().tg_bot_recipients or ""
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(int(piece))
        except ValueError:
            logger.warning("[bot_delivery] ignoring non-numeric recipient %r", piece)
    return out


def _make_bot() -> Bot:
    settings = get_settings()
    if not settings.tg_bot_token:
        raise BotDeliveryError("TG_BOT_TOKEN not set in .env")
    return Bot(token=settings.tg_bot_token)


async def _send_album_with_floodwait(bot: Bot, chat_id: int, media: list[InputMediaPhoto]):
    """send_media_group with one flood-wait retry — Telegram 429s are temporary, the match isn't."""
    import asyncio

    from aiogram.exceptions import TelegramRetryAfter

    if not 1 <= len(media) <= MAX_ALBUM:
        raise BotDeliveryError("photo delivery requires 1 to 10 photos")

    async def send():
        if len(media) == 1:
            first = media[0]
            return [await bot.send_photo(chat_id=chat_id, photo=first.media, caption=first.caption)]
        return await bot.send_media_group(chat_id=chat_id, media=media)

    try:
        return await send()
    except TelegramRetryAfter as e:
        wait = min(float(e.retry_after) + 1.0, 120.0)
        logger.warning(
            "[bot_delivery] flood-wait %ss for chat_id=%d — retrying once", wait, chat_id
        )
        await asyncio.sleep(wait)
        return await send()


async def send_match_via_bot(
    match_id: int,
    *,
    post_text_override: str | None = None,
    force_resend: bool = False,
) -> PublishedPost | None:
    """Send a match (caption + album) to every recipient in TG_BOT_RECIPIENTS.

    Returns the PublishedPost row created on the FIRST recipient's send (we record once per
    match; subsequent recipients are fan-out). Returns None on no-op (already-delivered).
    """
    async with get_session() as s:
        match = await s.get(Match, match_id)
        if match is None:
            raise BotDeliveryError(f"Match {match_id} not found")
        existing = (
            await s.execute(select(PublishedPost).where(PublishedPost.match_id == match_id))
        ).scalar_one_or_none()
        has_ledger = (await s.execute(select(MatchDelivery.id).where(
            MatchDelivery.match_id == match_id
        ).limit(1))).scalar_one_or_none() is not None
        incomplete = (await s.execute(select(MatchDelivery.id).where(
            MatchDelivery.match_id == match_id, MatchDelivery.status != "sent"
        ).limit(1))).scalar_one_or_none()
        if existing is not None and not force_resend and (not has_ledger or incomplete is None):
            logger.info(
                "Match %d historical delivery (msg=%d) — skip", match_id, existing.tg_message_id
            )
            return existing

        inbound = (
            await s.execute(
                select(InboundPost)
                .options(selectinload(InboundPost.source_channel))
                .where(InboundPost.id == match.inbound_post_id)
            )
        ).scalar_one_or_none()
        listing = await s.get(Listing, match.listing_id)
        if inbound is None or listing is None:
            raise BotDeliveryError(f"Match {match_id}: missing inbound/listing")

        saved_payload = None if force_resend else (await s.execute(
            select(MatchDelivery.payload).where(MatchDelivery.match_id == match_id)
            .order_by(MatchDelivery.id).limit(1)
        )).scalar_one_or_none()
        stored_paths = (
            saved_payload.get("photo_paths", []) if saved_payload else inbound.photo_paths
        )
        photo_paths = [Path(p) for p in stored_paths if p][:MAX_ALBUM]
        if not photo_paths:
            raise BotDeliveryError(f"Match {match_id}: no inbound photos on disk")
        if any(not p.is_file() for p in photo_paths):
            raise BotDeliveryError(f"Match {match_id}: missing inbound photo files")

    rendered_text = (
        post_text_override
        if post_text_override is not None
        else render_post(listing, inbound).text
    )

    source_link = None
    src_chan = inbound.source_channel
    if src_chan is not None and src_chan.username:
        source_link = f"https://t.me/{src_chan.username}/{inbound.tg_message_id}"

    header_lines = [
        f"🏠 Кандидат на @RentBelgrad — {listing.platform.value}",
        f"🔗 площадка: {listing.url}",
    ]
    if source_link:
        header_lines.append(f"📥 источник: {source_link}")
    header_lines.append(
        f"🎯 совпало фото: {match.matched_photo_count} "
        f"(conf {match.confidence:.2f}, {match.method})"
    )
    header_lines.append("")
    header_lines.append("─── готовый текст ↓ ───")
    header_lines.append("")
    full_caption = "\n".join(header_lines) + "\n" + rendered_text
    if len(full_caption) > TELEGRAM_CAPTION_LIMIT:
        full_caption = full_caption[:TELEGRAM_CAPTION_LIMIT - 1] + "…"

    recipients = _recipients()
    if not recipients:
        raise BotDeliveryError("TG_BOT_RECIPIENTS empty — no one to send to")

    bot = _make_bot()
    first_msg_id = 0
    delivered_to = 0
    preflight_error: BotDeliveryError | None = None
    try:
        await prepare_recipients(match_id, recipients, force=force_resend, payload={
            "caption": full_caption, "photo_paths": [str(p) for p in photo_paths],
        })
        for chat_id in dict.fromkeys(recipients):
            token = await claim_recipient(match_id, chat_id)
            if token is None:
                continue
            frozen = await claimed_payload(match_id, chat_id, token)
            full_caption = frozen["caption"]
            photo_paths = [Path(p) for p in frozen["photo_paths"]]
            if any(not p.is_file() for p in photo_paths):
                await finish_recipient(match_id, chat_id, token,
                                       error=ValueError("missing frozen photo files"))
                continue
            try:
                _preflight_photo_sizes(photo_paths)
            except BotDeliveryError as error:
                # Claim first so the durable outcome belongs to this worker and the exact
                # frozen payload; a local rejection is FAILED, never an ambiguous send.
                await finish_recipient(match_id, chat_id, token, error=error)
                preflight_error = error
                logger.warning(
                    "[bot_delivery] match=%d recipient=%d known_failure=%s: %s",
                    match_id, chat_id, type(error).__name__, error,
                )
                continue
            media: list[InputMediaPhoto] = []
            for j, p in enumerate(photo_paths):
                media.append(
                    InputMediaPhoto(
                        media=FSInputFile(str(p)),
                        caption=full_caption if j == 0 else None,
                    )
                )
            try:
                messages = await _send_album_with_floodwait(bot, chat_id, media)
            except Exception as error:
                await finish_recipient(match_id, chat_id, token, error=error)
                logger.warning(
                    "[bot_delivery] recipient=%d outcome=%s", chat_id, type(error).__name__
                )
                continue
            try:
                message_ids = [int(message.message_id) for message in messages]
            except (TypeError, AttributeError, ValueError):
                message_ids = []
            if len(message_ids) != len(media) or any(message_id <= 0 for message_id in message_ids):
                await finish_recipient(match_id, chat_id, token)
                continue
            # A DB failure after provider acceptance leaves the claim in sending, becoming
            # UNKNOWN on recovery. It must never be relabelled as a retryable transport error.
            await finish_recipient(match_id, chat_id, token, message_ids=message_ids)
            delivered_to += 1
            logger.info("Delivered match=%d recipient=%d photos=%d", match_id, chat_id, len(media))

    finally:
        await bot.session.close()

    async with get_session() as session:
        confirmed = (await session.execute(select(MatchDelivery).where(
            MatchDelivery.match_id == match_id, MatchDelivery.status == "sent"
        ).order_by(MatchDelivery.id))).scalars().all()
    if confirmed:
        first_msg_id = confirmed[0].message_ids[0]
        # A worker can lose every claim to another fanout. Its newly rendered text
        # must not replace the immutable caption that was actually delivered.
        full_caption = confirmed[0].payload["caption"]
    if not confirmed:
        if preflight_error is not None:
            raise preflight_error
        # NOT delivered anywhere — leave the match un-PUBLISHED so deliver-all / the next
        # pipeline run retries it. Marking it PUBLISHED here would silently lose the match.
        raise BotDeliveryError(
            f"Match {match_id}: delivery failed for all {len(recipients)} recipient(s)"
        )

    async with get_session() as s:
        m = await s.get(Match, match_id)
        if m is None:
            raise BotDeliveryError(f"Match {match_id} disappeared during delivery")
        existing = (
            await s.execute(select(PublishedPost).where(PublishedPost.match_id == match_id))
        ).scalar_one_or_none()
        if existing is not None and not force_resend:
            return existing
        if existing is not None:
            existing.tg_message_id = first_msg_id
            existing.post_text = full_caption
            published = existing
        else:
            inserted_id = (await s.execute(sqlite_insert(PublishedPost).values(
                match_id=match_id,
                tg_message_id=first_msg_id,
                post_text=full_caption,
            ).on_conflict_do_nothing(index_elements=["match_id"])
              .returning(PublishedPost.id))).scalar_one_or_none()
            published = (await s.execute(select(PublishedPost).where(
                PublishedPost.match_id == match_id
            ))).scalar_one()
            if inserted_id is None:
                # Another process committed this summary and its site overlay. Its
                # unique-row win is not a failed send and must not roll back ours.
                return published
            # The insert now owns SQLite's write lock. Re-read a human rejection
            # committed while this worker waited before changing the match status.
            await s.refresh(m)
        # PENDING→PUBLISHED only: never resurrect a REJECTED set by a human mid-send.
        m.status = resolve_post_send_status(m.status)
        # X1 auto-publish: expose the card on the site in THIS session — but only when the
        # match actually became PUBLISHED (a REJECTED-mid-send must never surface), and only
        # if the kill switch is on. Fully fault-isolated: auto-publish must NEVER break a send.
        if m.status == MatchStatus.PUBLISHED and get_settings().auto_publish_on_send:
            try:
                await auto_publish_site_listing(s, match_id)
            except Exception:
                logger.exception(
                    "[auto-publish] match=%d: auto-publish failed (delivery kept)", match_id
                )
        await s.flush()
        await s.refresh(published)
        return published


async def send_text_via_bot(text: str) -> None:
    """Fan out an arbitrary plain-text message to every TG_BOT_RECIPIENTS chat_id."""
    recipients = _recipients()
    bot = _make_bot()
    try:
        for chat_id in recipients:
            try:
                await bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                logger.exception("[bot_delivery] text send failed to %d", chat_id)
    finally:
        await bot.session.close()


async def send_run_summary_via_bot(
    *,
    matched: int,
    no_match: int,
    errors: int = 0,
    duration_seconds: float | None = None,
    label: str = "Прогон обработки",
) -> None:
    """Text-only run summary, fanned out to every recipient."""
    total = matched + no_match + errors
    lines = [
        f"📊 {label} — завершён",
        "",
        f"Обработано: {total}",
        f"✅ Найдено матчей: {matched}",
        f"❌ Без совпадения: {no_match}",
    ]
    if errors:
        lines.append(f"⚠️ Ошибки: {errors}")
    if duration_seconds is not None:
        if duration_seconds < 60:
            dur = f"{duration_seconds:.0f} сек"
        elif duration_seconds < 3600:
            dur = f"{int(duration_seconds // 60)} мин {int(duration_seconds % 60)} сек"
        else:
            h, rem = divmod(int(duration_seconds), 3600)
            dur = f"{h} ч {rem // 60} мин"
        lines.append(f"⏱ Время: {dur}")
    text = "\n".join(lines)

    recipients = _recipients()
    bot = _make_bot()
    try:
        for chat_id in recipients:
            try:
                await bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                logger.exception("[bot_delivery] summary send failed to %d", chat_id)
    finally:
        await bot.session.close()
    logger.info(
        "[bot_delivery] summary sent to %d recipient(s): matched=%d no_match=%d errors=%d",
        len(recipients), matched, no_match, errors,
    )


async def retry_pending_match_deliveries(*, limit: int = 30) -> dict[str, int]:
    """Resume only this ledger's known pending/failed recipients in the current bot contour.

    Historical PublishedPost rows never enter this queue. Unknown outcomes are reported for
    manual reconciliation; new configured recipients are not added to historical fanouts.
    """
    recipients = _recipients()
    if not recipients:
        return {"considered": 0, "completed": 0, "errors": 0, "unknown": 0}
    async with get_session() as session:
        rows = (await session.execute(select(MatchDelivery)
            .join(Match, Match.id == MatchDelivery.match_id).where(
            Match.status != MatchStatus.REJECTED,
            MatchDelivery.recipient_id.in_(recipients),
            MatchDelivery.status.in_(["pending", "failed", "sending", "unknown"]),
        ).order_by(MatchDelivery.updated_at, MatchDelivery.id))).scalars().all()
    unknown = sum(row.status == "unknown" for row in rows)
    now = datetime.now(UTC)
    from src.telegram.delivery_ledger import CLAIM_TIMEOUT
    eligible = [row for row in rows if row.status in {"pending", "failed"} or (
        row.status == "sending" and row.claimed_at is not None
        and row.claimed_at.replace(tzinfo=UTC) < now - CLAIM_TIMEOUT
    )]
    match_ids = list(dict.fromkeys(row.match_id for row in eligible))[:limit]
    stats = {"considered": len(match_ids), "completed": 0, "errors": 0, "unknown": unknown}
    for match_id in match_ids:
        try:
            await send_match_via_bot(match_id)
            async with get_session() as session:
                remaining = (await session.execute(select(MatchDelivery.id).where(
                    MatchDelivery.match_id == match_id, MatchDelivery.status != "sent"
                ).limit(1))).scalar_one_or_none()
            if remaining is None:
                stats["completed"] += 1
        except Exception as error:
            stats["errors"] += 1
            logger.warning("[delivery-retry] match=%d outcome=%s", match_id, type(error).__name__)
    return stats
