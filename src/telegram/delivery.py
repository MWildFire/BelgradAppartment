"""Deliver matched candidates to Darya via Telethon (userbot).

We do NOT publish to @RentBelgrad — Darya decides what to post. Our job ends when the
ready-to-paste template lands in her DMs (or Saved Messages, configurable) with the inbound
photos, a link to the listing on the platform, and a link to the original Telegram post.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telethon import TelegramClient

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

from src.telegram.run_summary import format_run_summary

logger = logging.getLogger(__name__)

MAX_ALBUM = 10
TELEGRAM_CAPTION_LIMIT = 1024


class DeliveryError(RuntimeError):
    """Raised when a match cannot be delivered."""


async def _resolve_delivery_target(client: TelegramClient):
    """Resolve the configured delivery recipient.

    Returns `await client.get_me()` if `TG_DELIVERY_TARGET` is empty or "me" (Saved Messages),
    otherwise looks up a user/chat by username or numeric ID. The userbot must be able to message
    that user — they must be in the userbot's contacts or have messaged the userbot first.
    """
    target = (get_settings().tg_delivery_target or "").strip().lstrip("@")
    if not target or target.lower() == "me":
        return await client.get_me()
    try:
        return await client.get_entity(target)
    except Exception as e:
        raise DeliveryError(
            f"Cannot resolve TG_DELIVERY_TARGET={target!r}: {type(e).__name__}: {e}. "
            "The userbot account must have that user as a contact, or that user must have "
            "messaged the userbot first. Add as a contact, or set TG_DELIVERY_TARGET=me "
            "to fall back to Saved Messages."
        ) from e


async def send_match_to_saved(
    match_id: int,
    client: TelegramClient | None,
    *,
    post_text_override: str | None = None,
    force_resend: bool = False,
) -> PublishedPost:
    """Dispatch to either the bot or userbot send path based on TG_DELIVERY_MODE.

    `client` is only required when mode == "userbot"; in bot mode it can be None.
    """
    mode = (get_settings().tg_delivery_mode or "userbot").lower()
    if mode == "bot":
        from src.telegram.bot_delivery import send_match_via_bot
        result = await send_match_via_bot(
            match_id,
            post_text_override=post_text_override,
            force_resend=force_resend,
        )
        if result is None:
            # No-op: already delivered, or rejected (never sent). Only a delivered match has a row.
            async with get_session() as s:
                existing = (
                    await s.execute(
                        select(PublishedPost).where(PublishedPost.match_id == match_id)
                    )
                ).scalar_one_or_none()
            if existing is None:
                raise DeliveryError(f"Match {match_id}: rejected; not sent")
            return existing
        return result
    if client is None:
        raise DeliveryError(
            "userbot delivery mode requires a Telethon client — pass one or set TG_DELIVERY_MODE=bot"
        )
    published = await _send_match_via_userbot(
        match_id, client,
        post_text_override=post_text_override,
        force_resend=force_resend,
    )
    if published is None:
        raise DeliveryError(f"Match {match_id}: rejected; not sent")
    return published


async def _send_match_via_userbot(
    match_id: int,
    client: TelegramClient,
    *,
    post_text_override: str | None = None,
    force_resend: bool = False,
) -> PublishedPost | None:
    """Send the inbound album + rendered post text to the user's own Saved Messages.

    Idempotent: if already delivered (PublishedPost exists), returns it without re-sending —
    unless `force_resend=True`. Marks Match.status = PUBLISHED on success (we reuse PUBLISHED
    semantically to mean "delivered to Darya — pending her manual repost into @RentBelgrad").
    A REJECTED match is never sent and returns None.
    """
    async with get_session() as s:
        match = await s.get(Match, match_id)
        if match is None:
            raise DeliveryError(f"Match {match_id} not found")
        if match.status == MatchStatus.REJECTED:
            return None
        existing = (
            await s.execute(
                select(PublishedPost).where(PublishedPost.match_id == match_id)
            )
        ).scalar_one_or_none()
        has_ledger = (await s.execute(select(MatchDelivery.id).where(
            MatchDelivery.match_id == match_id
        ).limit(1))).scalar_one_or_none() is not None
        if existing is not None and not has_ledger and not force_resend:
            logger.info(
                "Match %d historical delivery (msg=%d) — skipping", match_id, existing.tg_message_id
            )
            return existing

        # Eager-load source_channel so we can reach it after the session closes.
        inbound = (
            await s.execute(
                select(InboundPost)
                .options(selectinload(InboundPost.source_channel))
                .where(InboundPost.id == match.inbound_post_id)
            )
        ).scalar_one_or_none()
        listing = await s.get(Listing, match.listing_id)
        if inbound is None or listing is None:
            raise DeliveryError(f"Match {match_id}: missing inbound/listing")

        saved_payload = None if force_resend else (await s.execute(
            select(MatchDelivery.payload).where(MatchDelivery.match_id == match_id)
            .order_by(MatchDelivery.id).limit(1)
        )).scalar_one_or_none()
        stored_paths = (
            saved_payload.get("photo_paths", []) if saved_payload else inbound.photo_paths
        )
        photo_paths = [Path(p) for p in stored_paths if p][:MAX_ALBUM]
        if not photo_paths:
            raise DeliveryError(f"Match {match_id}: no inbound photos on disk")
        if any(not p.is_file() for p in photo_paths):
            raise DeliveryError(f"Match {match_id}: missing inbound photo files")

    rendered_text = post_text_override if post_text_override is not None else render_post(listing, inbound).text

    # Source link: t.me/<username>/<msg_id> — lets Darya re-check the original inbound post
    # before publishing. Requires the source channel to have a public username.
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

    recipient = await _resolve_delivery_target(client)
    recipient_id = int(recipient.id)
    await prepare_recipients(match_id, [recipient_id], force=force_resend, payload={
        "caption": full_caption, "photo_paths": [str(p) for p in photo_paths],
    })
    token = await claim_recipient(match_id, recipient_id)
    if token is None:
        async with get_session() as session:
            outcome = (await session.execute(select(MatchDelivery).where(
                MatchDelivery.match_id == match_id, MatchDelivery.recipient_id == recipient_id,
            ))).scalar_one_or_none()
        if outcome is None or outcome.status != "sent" or not outcome.message_ids:
            raise DeliveryError(f"Match {match_id}: delivery claimed or awaiting reconciliation")
        first_msg_id = outcome.message_ids[0]
    else:
        frozen = await claimed_payload(match_id, recipient_id, token)
        full_caption = frozen["caption"]
        photo_paths = [Path(p) for p in frozen["photo_paths"]]
        if any(not p.is_file() for p in photo_paths):
            error = ValueError("missing frozen photo files")
            await finish_recipient(match_id, recipient_id, token, error=error)
            raise DeliveryError(str(error))
        try:
            messages = await client.send_file(
                recipient, file=[str(p) for p in photo_paths], caption=full_caption,
            )
        except Exception as error:
            await finish_recipient(match_id, recipient_id, token, error=error)
            raise DeliveryError(f"Match {match_id}: {type(error).__name__}") from error
        messages = messages if isinstance(messages, list) else [messages]
        message_ids = [int(message.id) for message in messages if message is not None]
        if len(message_ids) != len(photo_paths) or any(
            message_id <= 0 for message_id in message_ids
        ):
            await finish_recipient(match_id, recipient_id, token)
            raise DeliveryError(f"Match {match_id}: provider did not confirm delivery")
        await finish_recipient(match_id, recipient_id, token, message_ids=message_ids)
        first_msg_id = message_ids[0]

    async with get_session() as s:
        m = await s.get(Match, match_id)
        if m is None:
            raise DeliveryError(f"Match {match_id} disappeared during delivery")
        # Re-check published row in case a concurrent send raced us.
        existing = (
            await s.execute(
                select(PublishedPost).where(PublishedPost.match_id == match_id)
            )
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
        # PENDING→PUBLISHED only: never resurrect a REJECTED set by a human mid-send.
        m.status = resolve_post_send_status(m.status)
        # X1 auto-publish (userbot path): mirror the bot path — expose on the site in THIS
        # session, only when the match actually became PUBLISHED and the kill switch is on,
        # fully fault-isolated so auto-publish can NEVER break a delivery.
        if m.status == MatchStatus.PUBLISHED and get_settings().auto_publish_on_send:
            try:
                await auto_publish_site_listing(s, match_id)
            except Exception:
                logger.exception("[auto-publish] match=%d: auto-publish failed (delivery kept)", match_id)
        await s.flush()
        await s.refresh(published)
        recipient_label = getattr(recipient, "username", None) or "me/SavedMessages"
        logger.info(
            "Delivered match=%d → @%s (msg=%d, %d photos, listing=%s/%s)",
            match_id, recipient_label, first_msg_id, len(photo_paths),
            listing.platform.value, listing.external_id,
        )
        return published


async def send_unmatched_to_saved(
    inbound_id: int,
    client: TelegramClient,
) -> None:
    """Forward an inbound post to Darya's Saved Messages even when no listing match was found.

    Idea: she still wants visibility into every new post from @OtzRentBelgrad and can either
    locate the listing manually on a platform, or wait until our DB grows to cover it.

    Dedup: we never re-send for an inbound — `InboundPost.status != NEW` after the first
    pipeline run, which `process_new_inbound` checks via the upstream caller.
    """
    async with get_session() as s:
        inbound = (
            await s.execute(
                select(InboundPost)
                .options(selectinload(InboundPost.source_channel))
                .where(InboundPost.id == inbound_id)
            )
        ).scalar_one_or_none()
        if inbound is None:
            return
        photo_paths = [Path(p) for p in inbound.photo_paths if p]
        photo_paths = [p for p in photo_paths if p.exists()][:MAX_ALBUM]
        if not photo_paths:
            logger.info("Inbound %d: no photos on disk — skipping unmatched delivery", inbound_id)
            return
        src = inbound.source_channel
        source_link = (
            f"https://t.me/{src.username}/{inbound.tg_message_id}"
            if src is not None and src.username
            else None
        )

    header = (
        "❓ Совпадение на площадках не найдено\n"
        + (f"🔗 источник: {source_link}\n" if source_link else "")
        + "Можно проверить руками или подождать пополнения БД listings.\n\n"
        + "─── оригинальный текст ───\n\n"
    )
    raw = (inbound.raw_text or "").strip()
    # Telegram caption limit ≈ 1024
    caption = (header + raw)[:1020] + ("…" if len(header + raw) > 1020 else "")

    recipient = await _resolve_delivery_target(client)
    await client.send_file(recipient, file=[str(p) for p in photo_paths], caption=caption)
    logger.info(
        "Forwarded unmatched inbound %d to %s (%d photos)",
        inbound_id, getattr(recipient, "username", None) or "me", len(photo_paths),
    )


async def send_run_summary(
    client: TelegramClient,
    *,
    matched: int,
    no_match: int,
    errors: int = 0,
    needs_review: int = 0,
    pending: int = 0,
    unresolved: int = 0,
    selected: int | None = None,
    duration_seconds: float | None = None,
    label: str = "Прогон обработки",
) -> None:
    """Send a text-only run summary to the configured recipient.

    Called at the end of every batch operation (`backfill`, `match-all`, `rematch-all`,
    `weekly-refresh`, manual smoke runs) so Дарья sees a one-line «here's what just happened»
    after a wave of activity, even if zero matches were found (no_match-only run still gets a
    receipt — that's the difference from the per-inbound flow, where silence is the signal).
    """
    # Bot mode: fan out to every TG_BOT_RECIPIENTS chat_id.
    if (get_settings().tg_delivery_mode or "userbot").lower() == "bot":
        from src.telegram.bot_delivery import send_run_summary_via_bot
        await send_run_summary_via_bot(
            matched=matched, no_match=no_match, errors=errors,
            needs_review=needs_review, pending=pending, unresolved=unresolved, selected=selected,
            duration_seconds=duration_seconds, label=label,
        )
        return

    text = format_run_summary(
        matched=matched, no_match=no_match, errors=errors, needs_review=needs_review,
        pending=pending, unresolved=unresolved, selected=selected,
        duration_seconds=duration_seconds, label=label,
    )
    recipient = await _resolve_delivery_target(client)
    await client.send_message(recipient, text)
    logger.info(
        "[summary] sent run summary: matched=%d no_match=%d errors=%d → @%s",
        matched, no_match, errors,
        getattr(recipient, "username", None) or "me",
    )


async def send_text_to_recipients(client: TelegramClient | None, text: str) -> None:
    """Push a plain-text message to the configured recipients (bot fan-out OR userbot Saved Messages).

    Used by maintenance jobs (e.g. nightly-refresh) that need a free-form text body rather than
    the matched/no_match/errors layout of `send_run_summary`.
    """
    if (get_settings().tg_delivery_mode or "userbot").lower() == "bot":
        from src.telegram.bot_delivery import send_text_via_bot
        await send_text_via_bot(text)
        return
    if client is None:
        raise DeliveryError("userbot mode requires a Telethon client to send_text_to_recipients")
    recipient = await _resolve_delivery_target(client)
    await client.send_message(recipient, text)
