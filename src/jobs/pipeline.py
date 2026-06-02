"""End-to-end pipeline that fires for each new inbound post.

Steps:
1. Refresh listings on all platforms (`scrape_all_with_throttle` — skips if last run <15 min ago).
2. Run pHash matcher (`match_inbound`).
3. If matched, deliver the ready-to-post template + photos to Darya's Saved Messages
   via the existing Telethon client (no Bot API).

The pipeline is best-effort: a scrape failure does not block matching, and a delivery failure
does not block subsequent inbound posts. Everything is logged.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient

from src.config import get_settings
from src.matching.matcher import MatchResult, match_inbound
from src.scrapers.orchestrator import scrape_all_with_throttle
from src.telegram.delivery import send_match_to_saved
from src.telegram.telemetry import notify_no_match

logger = logging.getLogger(__name__)


async def process_new_inbound(
    inbound_id: int,
    *,
    client: TelegramClient | None = None,
    refresh_listings: bool = True,
) -> MatchResult:
    """Run the full inbound → match → deliver pipeline for one post.

    **Only matched candidates are forwarded to Darya's bot.** Unmatched posts stay in the DB
    with `InboundStatus.NO_MATCH` for audit/replay; they're surfaced via a compact one-line
    telemetry message into the logger userbot's Saved Messages (see `notify_no_match`) so we
    can tell "pipeline alive, no hits" from "pipeline dead".
    """
    settings = get_settings()

    if refresh_listings:
        try:
            ran, _results = await scrape_all_with_throttle()
            if ran:
                logger.info("[pipeline] refreshed listings before matching inbound=%d", inbound_id)
        except Exception:
            logger.exception("[pipeline] scrape failed (continuing to match)")

    result = await match_inbound(inbound_id)

    # Decide whether we can deliver. Bot mode goes through the Bot API (Telethon client unused),
    # so client=None is fine. Userbot mode genuinely needs a Telethon client.
    mode = (settings.tg_delivery_mode or "userbot").lower()
    if mode == "userbot" and client is None:
        return result

    if result.status == "matched" and result.match_id is not None:
        try:
            await send_match_to_saved(result.match_id, client)
        except Exception:
            logger.exception("[pipeline] matched delivery failed for match=%s", result.match_id)
    else:
        logger.info(
            "[pipeline] inbound=%s no_match (best_count=%d conf=%.2f) — not forwarded",
            inbound_id, result.matched_photos, result.confidence,
        )
        # Fire-and-forget: returns immediately, send happens in a background task.
        notify_no_match(
            inbound_id,
            best_count=result.matched_photos,
            threshold=settings.min_matched_photos,
            method=result.method,
            confidence=result.confidence,
        )

    return result
