"""Single long-polling daemon for RentFinderBot (`otz bot`, unit deploy/otz-bot.service).

A bot token can have only ONE getUpdates consumer: a second poller gets
TelegramConflictError. So every interactive bot feature lives in one Dispatcher and is
switched on by flags:
- WATERMARK_BOT_ENABLED    - /clean and incoming photos (src/telegram/wm_bot.py);
- SUBSCRIPTION_BOT_ENABLED - /start sub_<token> site subscriptions (subscription_bot.py).
With WATERMARK_BOT_ENABLED and WATERMARK_SITE_USE_CLEAN both on, the daemon also keeps clean
copies of public site photos up to date (src/watermark/published.py, every 10 minutes).
Candidate delivery (bot_delivery.py) only sends messages and does not need this daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from src.config import get_settings

logger = logging.getLogger(__name__)


def build_dispatcher() -> Any:
    from aiogram import Dispatcher

    from src.telegram.subscription_bot import build_subscription_router
    from src.telegram.wm_bot import allowed_chat_ids, build_watermark_router

    settings = get_settings()
    dp = Dispatcher()
    if settings.watermark_bot_enabled:
        allowed = allowed_chat_ids()
        if not allowed:
            logger.warning("[bot] WATERMARK_BOT_ENABLED but no allowed chat ids: /clean disabled")
        else:
            dp.include_router(build_watermark_router(allowed))
    if settings.subscription_bot_enabled:
        dp.include_router(build_subscription_router())
    return dp


async def run_bot_daemon() -> None:
    """Blocks until stopped. Does nothing when both features are disabled."""
    from aiogram import Bot

    settings = get_settings()
    if not (settings.watermark_bot_enabled or settings.subscription_bot_enabled):
        logger.warning("[bot] nothing enabled: WATERMARK_BOT_ENABLED=SUBSCRIPTION_BOT_ENABLED=0")
        return
    if not settings.tg_bot_token:
        raise RuntimeError("TG_BOT_TOKEN not set - cannot run the bot daemon")
    dp = build_dispatcher()
    bot = Bot(token=settings.tg_bot_token)
    sweep: asyncio.Task[None] | None = None
    if settings.watermark_bot_enabled and settings.watermark_site_use_clean:
        from src.telegram.wm_bot import run_exclusive
        from src.watermark.published import sweep_forever

        sweep = asyncio.create_task(sweep_forever(run_exclusive))
    try:
        # Keep updates that queued up while the daemon was down: /start links of subscriptions
        # must not be lost. Old photos are skipped by wm_bot (MAX_MEDIA_AGE) instead.
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("[bot] long-polling started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if sweep is not None:
            sweep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep
        await bot.session.close()
