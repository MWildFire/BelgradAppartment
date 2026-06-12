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
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InputMediaPhoto
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.config import get_settings
from src.db import (
    InboundPost,
    Listing,
    Match,
    MatchStatus,
    PublishedPost,
    get_session,
)
from src.post import render_post

logger = logging.getLogger(__name__)

MAX_ALBUM = 10
TELEGRAM_CAPTION_LIMIT = 1024


class BotDeliveryError(RuntimeError):
    """Raised when a match cannot be delivered through the Bot API."""


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

    try:
        return await bot.send_media_group(chat_id=chat_id, media=media)
    except TelegramRetryAfter as e:
        wait = min(float(e.retry_after) + 1.0, 120.0)
        logger.warning("[bot_delivery] flood-wait %ss for chat_id=%d — retrying once", wait, chat_id)
        await asyncio.sleep(wait)
        return await bot.send_media_group(chat_id=chat_id, media=media)


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
        if existing is not None and not force_resend:
            logger.info("Match %d already delivered (msg=%d) — skip", match_id, existing.tg_message_id)
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

        photo_paths = [Path(p) for p in inbound.photo_paths if p]
        photo_paths = [p for p in photo_paths if p.exists()][:MAX_ALBUM]
        if not photo_paths:
            raise BotDeliveryError(f"Match {match_id}: no inbound photos on disk")

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
        f"🎯 совпало фото: {match.matched_photo_count} (conf {match.confidence:.2f}, {match.method})"
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
    try:
        for chat_id in recipients:
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
                if delivered_to == 0 and messages:
                    first_msg_id = int(messages[0].message_id)
                delivered_to += 1
                logger.info(
                    "Delivered match=%d → chat_id=%d (msg=%d, %d photos)",
                    match_id, chat_id,
                    int(messages[0].message_id) if messages else 0,
                    len(photo_paths),
                )
            except Exception:
                # One blocked/broken recipient must not stop the fan-out to the others.
                logger.exception("[bot_delivery] failed to send to %d", chat_id)
    finally:
        await bot.session.close()

    if delivered_to == 0:
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
            published = PublishedPost(
                match_id=match_id,
                tg_message_id=first_msg_id,
                post_text=full_caption,
            )
            s.add(published)
        m.status = MatchStatus.PUBLISHED
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
