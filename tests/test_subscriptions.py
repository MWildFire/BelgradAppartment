"""C2 — subscription writer: create (email/telegram), honeypot, unsubscribe, /start linking."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import Base, Subscription, SubscriptionMatch  # noqa: F401
from src.site.subscriptions import (
    SubscriptionPayload,
    SubscriptionValidationError,
    create_subscription,
    unsubscribe,
)


@pytest_asyncio.fixture
async def sub_db(tmp_path: Path):
    """File-backed empty DB url with schema created (the site writer uses a file DB)."""
    db_path = tmp_path / "sub.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    yield url


async def _all_subs(url: str) -> list[Subscription]:
    from src.site import db as sitedb

    async with sitedb.read_session(database_url=url) as s:
        return list((await s.execute(select(Subscription).order_by(Subscription.id))).scalars())


# ----------------------------------------------------------------- create: e-mail


@pytest.mark.asyncio
async def test_create_email_subscription_active_with_token(sub_db: str) -> None:
    payload = SubscriptionPayload(
        channel="email", email="a@b.com", districts=["Врачар"], price_max=800
    )
    result = await create_subscription(payload, database_url=sub_db)
    assert result is not None
    rows = await _all_subs(sub_db)
    assert len(rows) == 1
    sub = rows[0]
    assert sub.channel == "email"
    assert sub.email == "a@b.com"
    assert sub.is_active is True
    assert sub.telegram_chat_id is None
    assert sub.token  # random, non-empty
    assert sub.criteria["district"] == ["Врачар"]
    assert sub.criteria["price_max"] == 800
    # The deep-link is only meaningful for telegram subs.
    assert result.telegram_deep_link is None


# ----------------------------------------------------------------- create: telegram


@pytest.mark.asyncio
async def test_create_telegram_subscription_inactive_until_linked(
    sub_db: str, monkeypatch
) -> None:
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "site_telegram_bot_username", "RentBelgradBot")
    payload = SubscriptionPayload(channel="telegram", districts=["Земун"])
    result = await create_subscription(payload, database_url=sub_db)
    assert result is not None
    sub = (await _all_subs(sub_db))[0]
    assert sub.channel == "telegram"
    assert sub.is_active is False  # not linked yet
    assert sub.telegram_chat_id is None
    assert sub.email is None
    # Deep-link carries the token so the bot can link the chat on /start.
    assert result.telegram_deep_link == f"https://t.me/RentBelgradBot?start=sub_{sub.token}"


# ----------------------------------------------------------------- honeypot


@pytest.mark.asyncio
async def test_honeypot_filled_is_silent_noop(sub_db: str) -> None:
    payload = SubscriptionPayload(channel="email", email="bot@x.com", company="ACME")
    result = await create_subscription(payload, database_url=sub_db)
    assert result is None  # silent drop
    assert await _all_subs(sub_db) == []


# ----------------------------------------------------------------- validation


@pytest.mark.asyncio
async def test_email_channel_requires_email(sub_db: str) -> None:
    with pytest.raises(SubscriptionValidationError):
        await create_subscription(
            SubscriptionPayload(channel="email", email=""), database_url=sub_db
        )


@pytest.mark.asyncio
async def test_email_must_look_like_email(sub_db: str) -> None:
    with pytest.raises(SubscriptionValidationError):
        await create_subscription(
            SubscriptionPayload(channel="email", email="not-an-email"), database_url=sub_db
        )


@pytest.mark.asyncio
async def test_unknown_channel_rejected(sub_db: str) -> None:
    with pytest.raises(SubscriptionValidationError):
        await create_subscription(
            SubscriptionPayload(channel="sms", email="a@b.com"), database_url=sub_db
        )


@pytest.mark.asyncio
async def test_telegram_needs_configured_bot_username(sub_db: str, monkeypatch) -> None:
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "site_telegram_bot_username", "")
    with pytest.raises(SubscriptionValidationError):
        await create_subscription(
            SubscriptionPayload(channel="telegram"), database_url=sub_db
        )


# ----------------------------------------------------------------- unsubscribe


@pytest.mark.asyncio
async def test_unsubscribe_flips_is_active(sub_db: str) -> None:
    payload = SubscriptionPayload(channel="email", email="a@b.com")
    result = await create_subscription(payload, database_url=sub_db)
    token = (await _all_subs(sub_db))[0].token

    ok = await unsubscribe(token, database_url=sub_db)
    assert ok is True
    assert (await _all_subs(sub_db))[0].is_active is False
    # Unknown token is a no-op (returns False, doesn't raise).
    assert await unsubscribe("nope", database_url=sub_db) is False
