"""C2 — saved-search subscription writer (mirrors src/site/leads.py).

Pure write-path: validate criteria + channel, drop honeypot bots silently, mint a random token,
insert one Subscription through the narrow writer engine (busy_timeout + retry on a transient
lock). State machine:
- channel="email": is_active=True immediately (the e-mail IS the contact).
- channel="telegram": is_active=False with no chat_id; the deep-link carries the token so the
  linking bot's /start handler can set telegram_chat_id + is_active=True later.

CSRF / rate-limit live in the route (as for leads), not here. Token-based unsubscribe (no auth)
flips is_active=False for either channel.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from src.config import get_settings
from src.db.models import Subscription
from src.site.db import writer_session

logger = logging.getLogger(__name__)

VALID_CHANNELS = ("email", "telegram")
EMAIL_MAX = 254
# Pragmatic e-mail shape check (defence against typos/garbage, not RFC-complete validation).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_WRITE_RETRIES = 3
_WRITE_RETRY_DELAY = 0.2


class SubscriptionValidationError(ValueError):
    """Bad channel / contact / criteria. The route maps it to 400."""


@dataclass(frozen=True)
class SubscriptionPayload:
    """Form fields from POST /subscribe. `company` is the anti-bot honeypot (as for leads)."""

    channel: str  # "email" | "telegram"
    email: str | None = None
    districts: list[str] = field(default_factory=list)
    price_min: int | None = None
    price_max: int | None = None
    rooms: list[str] = field(default_factory=list)
    vnj: bool = False
    white_card: bool = False
    company: str | None = None  # honeypot: real users never see/fill it


@dataclass(frozen=True)
class SubscriptionResult:
    """What create_subscription returns: the saved row's id/token + the optional deep-link.

    telegram_deep_link is set ONLY for a telegram subscription (https://t.me/<bot>?start=sub_…);
    e-mail subscriptions get None.
    """

    id: int
    token: str
    channel: str
    telegram_deep_link: str | None


def _criteria_dict(payload: SubscriptionPayload) -> dict[str, object]:
    """Serialize criteria mirroring readmodel.Filters (so the matcher rebuilds Filters as-is)."""
    return {
        "vnj": bool(payload.vnj),
        "wc": bool(payload.white_card),
        "district": [d for d in payload.districts if d and d.strip()],
        "price_min": payload.price_min,
        "price_max": payload.price_max,
        "rooms": [r for r in payload.rooms if r and r.strip()],
    }


def _validate(payload: SubscriptionPayload) -> None:
    if payload.channel not in VALID_CHANNELS:
        raise SubscriptionValidationError(f"channel must be one of {VALID_CHANNELS}")

    if payload.price_min is not None and payload.price_min < 0:
        raise SubscriptionValidationError("price_min must be >= 0")
    if payload.price_max is not None and payload.price_max < 0:
        raise SubscriptionValidationError("price_max must be >= 0")

    if payload.channel == "email":
        email = (payload.email or "").strip()
        if not email or len(email) > EMAIL_MAX or not _EMAIL_RE.match(email):
            raise SubscriptionValidationError("a valid e-mail is required for the email channel")

    if payload.channel == "telegram":
        # Telegram linking needs the bot username to build the deep-link — refuse if unset so the
        # visitor isn't handed a dead /start link.
        if not get_settings().site_telegram_bot_username.strip():
            raise SubscriptionValidationError(
                "telegram channel is unavailable — site_telegram_bot_username not configured"
            )


async def _insert(payload: SubscriptionPayload, database_url: str | None) -> Subscription:
    """Insert one Subscription through the narrow writer, retrying on a transient sqlite lock."""
    is_active = payload.channel == "email"  # telegram starts inactive until /start links a chat
    last_err: OperationalError | None = None
    for attempt in range(_WRITE_RETRIES):
        try:
            async with writer_session(database_url=database_url) as s:
                sub = Subscription(
                    criteria=_criteria_dict(payload),
                    channel=payload.channel,
                    email=(payload.email or "").strip() or None
                    if payload.channel == "email"
                    else None,
                    telegram_chat_id=None,
                    token=secrets.token_urlsafe(24),
                    is_active=is_active,
                )
                s.add(sub)
                await s.flush()
                await s.refresh(sub)
                return sub
        except OperationalError as e:
            if "database is locked" not in str(e).lower():
                raise
            last_err = e
            logger.warning(
                "subscription insert hit a locked DB (attempt %d) — retrying", attempt + 1
            )
            await asyncio.sleep(_WRITE_RETRY_DELAY)
    assert last_err is not None
    raise last_err


async def create_subscription(
    payload: SubscriptionPayload, *, database_url: str | None = None
) -> SubscriptionResult | None:
    """Persist a subscription. Returns the result, or None when the honeypot is tripped.

    Raises SubscriptionValidationError on bad input. For a telegram subscription the result
    carries the deep-link the visitor must open to link their chat.
    """
    # Honeypot first — a bot's spam should leave no trace.
    if payload.company and payload.company.strip():
        logger.info("subscription honeypot tripped — dropping silently")
        return None

    _validate(payload)
    sub = await _insert(payload, database_url)

    deep_link = None
    if payload.channel == "telegram":
        username = get_settings().site_telegram_bot_username.strip()
        deep_link = f"https://t.me/{username}?start=sub_{sub.token}"

    return SubscriptionResult(
        id=sub.id, token=sub.token, channel=sub.channel, telegram_deep_link=deep_link
    )


async def unsubscribe(token: str, *, database_url: str | None = None) -> bool:
    """Token-based unsubscribe: flip is_active=False. Returns True if a row matched, else False.

    Idempotent and auth-free — an unknown/blank token is a silent no-op (returns False).
    """
    token = (token or "").strip()
    if not token:
        return False
    async with writer_session(database_url=database_url) as s:
        existing = (
            await s.execute(select(Subscription.id).where(Subscription.token == token))
        ).scalar_one_or_none()
        if existing is None:
            return False
        await s.execute(
            update(Subscription).where(Subscription.token == token).values(is_active=False)
        )
        return True
