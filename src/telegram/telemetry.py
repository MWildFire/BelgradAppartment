"""Lightweight per-event telemetry into Saved Messages via the logger userbot.

Uses a SEPARATE Telethon session (`otz_logger.session`) so this can run concurrently
with the live `otz-monitor.service` — same pattern as `ship-log`. Each call opens a
short-lived client, sends one message, closes.

Public entry point `notify_no_match` is fire-and-forget: it schedules the send as a
background task and returns immediately, so the pipeline isn't blocked by Telethon's
~1 s handshake. Failures are logged but never raised — telemetry must never break the
pipeline. An `asyncio.Lock` serialises actual sends within the same process so two
concurrent inbound events don't race on the same SQLite session file.

Cross-process race (e.g. `otz-monitor.service` + `otz-bg` adhoc) is still possible
but rare and the only consequence is a single dropped notify — see HANDOFF §13.1.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telethon import TelegramClient

from src.config import get_settings
from src.db import InboundPost, get_session
from src.matching.matcher import METHOD_CLIP_LLM_REJECTED

logger = logging.getLogger(__name__)

# Serialises sends within one process. The underlying SQLite session file (otz_logger)
# does not tolerate concurrent opens.
_send_lock = asyncio.Lock()

# Avoid spamming the log with the same "credentials missing" warning on every no_match.
_creds_warning_emitted = False


def notify_no_match(
    inbound_id: int,
    *,
    best_count: int,
    threshold: int,
    method: str,
    confidence: float,
) -> asyncio.Task[None]:
    """Schedule a no_match notification as a background task and return immediately.

    Without this, unmatched inbounds are invisible — the only signals are `monitor.log`
    and the daily 07:00 UTC summary, so a silent pipeline failure is indistinguishable
    from "no matches today".
    """
    return asyncio.create_task(
        _do_notify_no_match(
            inbound_id,
            best_count=best_count,
            threshold=threshold,
            method=method,
            confidence=confidence,
        )
    )


async def _do_notify_no_match(
    inbound_id: int,
    *,
    best_count: int,
    threshold: int,
    method: str,
    confidence: float,
) -> None:
    """Build the message, open a short-lived logger client, and send to Saved Messages."""
    global _creds_warning_emitted
    settings = get_settings()
    if not settings.notify_no_match_enabled:
        return
    if not settings.tg_api_id or not settings.tg_api_hash:
        if not _creds_warning_emitted:
            logger.warning(
                "[telemetry] TG_API_ID / TG_API_HASH not set — no_match notify disabled "
                "(will not repeat this warning)"
            )
            _creds_warning_emitted = True
        return

    try:
        body = await _build_message(inbound_id, best_count, threshold, method, confidence)
        if body is None:
            return
        session_path = str(settings.data_dir / "otz_logger")
        async with _send_lock:
            async with TelegramClient(session_path, settings.tg_api_id, settings.tg_api_hash) as client:
                await client.send_message("me", body)
        logger.info("[telemetry] notify no_match inbound=%s → Saved", inbound_id)
    except Exception:
        logger.exception("[telemetry] failed to notify no_match inbound=%s", inbound_id)


async def _build_message(
    inbound_id: int,
    best_count: int,
    threshold: int,
    method: str,
    confidence: float,
) -> str | None:
    """Render the one-liner. Returns None if the inbound row vanished mid-flight."""
    async with get_session() as s:
        inbound = (
            await s.execute(
                select(InboundPost)
                .options(selectinload(InboundPost.source_channel))
                .where(InboundPost.id == inbound_id)
            )
        ).scalar_one_or_none()
        if inbound is None:
            return None
        src = inbound.source_channel
        source_link = (
            f"https://t.me/{src.username}/{inbound.tg_message_id}"
            if src is not None and src.username
            else None
        )
        raw = (inbound.raw_text or "").strip()
        first_line = raw.splitlines()[0][:80] if raw else "(без текста)"
        price = inbound.parsed_price_eur
        area = inbound.parsed_area_m2

    reason = (
        "LLM отклонил кандидата"
        if method == METHOD_CLIP_LLM_REJECTED
        else "ниже порога photo-match"
    )

    lines = [
        f"📭 inbound #{inbound_id} — no_match",
        f"причина: {reason}",
        f"фото-совпало: {best_count}/{threshold} · conf {confidence:.2f} · {method}",
    ]
    meta = []
    if price:
        meta.append(f"{price}€")
    if area:
        meta.append(f"{area:g}м²")
    meta_str = " / ".join(meta) + " — " if meta else ""
    lines.append(f"пост: {meta_str}{first_line}")
    if source_link:
        lines.append(source_link)
    return "\n".join(lines)
