"""C2 — Telegram linking daemon for saved-search subscriptions (config-gated, OFF by default).

This is a NEW long-running aiogram Dispatcher that polls for `/start sub_<token>` and links the
sender's chat to the subscription that owns that token (sets telegram_chat_id + is_active=True).
It is deliberately ISOLATED from the send path (bot_delivery.py / subscription_delivery.py, which
construct a Bot per send) so adding a poller can't perturb delivery.

The daemon only runs when `subscription_bot_enabled` is True — enabling it is a deploy choice
(it needs its own systemd unit). The `/start` business logic lives in `handle_start`, a pure
async function unit-tested with a fake message (no live bot / no network).
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update

from src.config import get_settings
from src.db.models import Subscription
from src.site import db as sitedb

logger = logging.getLogger(__name__)

_START_PREFIX = "sub_"

_LINKED_OK = (
    "✅ Подписка активирована! Вы будете получать уведомления о новых квартирах "
    "по вашим критериям сюда. Чтобы отписаться — перейдите по ссылке «Отписаться» "
    "в любом сообщении."
)
_LINKED_FAIL = (
    "Не удалось найти вашу подписку по этой ссылке. Попробуйте оформить подписку "
    "на сайте заново."
)
_NO_TOKEN = (
    "Привет! Это бот уведомлений о квартирах RentBelgrad. Чтобы подключить уведомления, "
    "оформите подписку на сайте и перейдите по выданной ссылке."
)


def _extract_token(text: str | None) -> str | None:
    """Parse `/start sub_<token>` → token, else None (plain /start or no payload)."""
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0] != "/start":
        return None
    payload = parts[1].strip()
    if not payload.startswith(_START_PREFIX):
        return None
    return payload[len(_START_PREFIX):] or None


async def handle_start(message, *, database_url: str | None = None) -> bool:
    """Handle a /start: link the sender's chat to the subscription by token.

    Returns True iff a subscription was linked. Always replies (graceful for unknown/blank
    tokens). `message` is any object with `.text`, `.chat.id`, and an async `.answer(text)` —
    the real aiogram Message satisfies this, and tests pass a tiny fake (no live bot needed).
    """
    token = _extract_token(getattr(message, "text", None))
    if token is None:
        await message.answer(_NO_TOKEN)
        return False

    chat_id = int(message.chat.id)
    async with sitedb.writer_session(database_url=database_url) as s:
        sub_id = (
            await s.execute(
                select(Subscription.id).where(
                    Subscription.token == token, Subscription.channel == "telegram"
                )
            )
        ).scalar_one_or_none()
        if sub_id is None:
            await message.answer(_LINKED_FAIL)
            return False
        await s.execute(
            update(Subscription)
            .where(Subscription.id == sub_id)
            .values(telegram_chat_id=chat_id, is_active=True)
        )

    await message.answer(_LINKED_OK)
    logger.info("[subscription-bot] linked chat_id=%d to subscription token=%s", chat_id, token)
    return True


async def run_subscription_bot() -> None:
    """Run the aiogram long-polling Dispatcher (blocks). Gated by subscription_bot_enabled.

    Imported lazily inside so the heavy aiogram Dispatcher machinery only loads when the daemon
    is actually started — the send path and the unit-tested handle_start never trigger it.
    """
    from aiogram import Bot, Dispatcher
    from aiogram.filters import CommandStart

    settings = get_settings()
    if not settings.subscription_bot_enabled:
        logger.warning("[subscription-bot] disabled (SUBSCRIPTION_BOT_ENABLED=0) — not starting")
        return
    if not settings.tg_bot_token:
        raise RuntimeError("TG_BOT_TOKEN not set — cannot run the subscription linking bot")

    bot = Bot(token=settings.tg_bot_token)
    dp = Dispatcher()

    @dp.message(CommandStart(deep_link=True))
    async def _on_start_with_payload(message):  # pragma: no cover - exercised live only
        await handle_start(message)

    @dp.message(CommandStart())
    async def _on_plain_start(message):  # pragma: no cover - exercised live only
        await handle_start(message)

    logger.info("[subscription-bot] starting long-polling for /start linking")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
