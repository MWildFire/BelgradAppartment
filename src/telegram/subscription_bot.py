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
_LINKED_ALREADY = (
    "Эта подписка уже привязана к другому Telegram-аккаунту. Если это были вы — "
    "отпишитесь по ссылке в сообщении и оформите подписку заново."
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
        row = (
            await s.execute(
                select(Subscription.id, Subscription.telegram_chat_id).where(
                    Subscription.token == token, Subscription.channel == "telegram"
                )
            )
        ).one_or_none()
        if row is None:
            await message.answer(_LINKED_FAIL)
            return False
        sub_id, existing_chat = row
        # Ownership guard: the token is printed in EVERY digest (the unsubscribe link), so a
        # leaked token must NOT let a different chat hijack the subscription. Allow the first
        # link or a re-link from the SAME chat (idempotent); refuse re-link to a different chat.
        if existing_chat is not None and int(existing_chat) != chat_id:
            await message.answer(_LINKED_ALREADY)
            # Log the subscription id, NOT the token — the token is the sole secret guarding the
            # subscription (it rides in every digest's unsubscribe link).
            logger.warning(
                "[subscription-bot] refused relink of subscription id=%d to a different chat_id",
                sub_id,
            )
            return False
        await s.execute(
            update(Subscription)
            .where(Subscription.id == sub_id)
            .values(telegram_chat_id=chat_id, is_active=True)
        )

    await message.answer(_LINKED_OK)
    logger.info("[subscription-bot] linked chat_id=%d to subscription id=%d", chat_id, sub_id)
    return True


def build_subscription_router():
    """aiogram Router with /start subscription linking (included by src/telegram/bot_daemon.py)."""
    from aiogram import Router
    from aiogram.filters import CommandStart

    router = Router(name="subscription")

    @router.message(CommandStart(deep_link=True))
    async def _on_start_with_payload(message):  # pragma: no cover - exercised live only
        await handle_start(message)

    @router.message(CommandStart())
    async def _on_plain_start(message):  # pragma: no cover - exercised live only
        await handle_start(message)

    return router


async def run_subscription_bot() -> None:
    """Compatibility for the old `otz subscription-bot` command: it starts no poller any more.

    A bot token allows only one poller: /start subscription linking lives in
    src/telegram/bot_daemon.py (`otz bot`) together with /clean. A leftover unit must not
    become a second poller, so this only logs a warning.
    """
    logger.warning("[subscription-bot] replaced by otz bot (otz-bot.service), not starting")
    return
