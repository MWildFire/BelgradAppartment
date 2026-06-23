"""C2 — saved-search digest delivery over Telegram (SEND-ONLY).

Reuses the existing `Bot(...)`-per-send pattern from bot_delivery.py: a digest is one text
message to one chat_id (the subscriber's linked chat). Kept separate from the linking daemon
(src/telegram/subscription_bot.py) so the send path never imports a Dispatcher.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from aiogram import Bot

from src.config import get_settings

if TYPE_CHECKING:
    from src.site.readmodel import SiteListingView

logger = logging.getLogger(__name__)


def _digest_text(listings: Sequence[SiteListingView], unsubscribe_url: str) -> str:
    settings = get_settings()
    base = (settings.site_public_base_url or "").rstrip("/")
    lines = [f"🏠 Новые квартиры по вашей подписке ({len(listings)}):", ""]
    for v in listings:
        price = f"{v.price_eur}€" if v.price_eur else "—"
        url = f"{base}/apt/{v.id}" if base else f"/apt/{v.id}"
        lines.append(f"• {price}, {v.rooms}, {v.district} — {url}")
    lines.append("")
    lines.append(f"Отписаться: {unsubscribe_url}")
    return "\n".join(lines)


async def send_subscription_digest_telegram(
    chat_id: int | None,
    listings: Sequence[SiteListingView],
    unsubscribe_url: str,
) -> None:
    """Send one digest message to `chat_id`. No-op for an empty digest or unconfigured bot.

    Raises on a real send failure so the matcher counts it and skips recording the ledger.
    """
    if not listings:
        return
    if chat_id is None:
        logger.warning("subscription telegram digest requested with no chat_id — skipping")
        return
    settings = get_settings()
    if not settings.tg_bot_token:
        logger.info("TG_BOT_TOKEN not set, skipping subscription telegram digest")
        return

    bot = Bot(token=settings.tg_bot_token)
    try:
        await bot.send_message(chat_id=chat_id, text=_digest_text(listings, unsubscribe_url))
    finally:
        await bot.session.close()
