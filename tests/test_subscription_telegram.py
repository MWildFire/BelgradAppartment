"""C2 — subscription digest Telegram delivery + /start linking (both with aiogram mocked)."""

from __future__ import annotations

import pytest

from src.site.readmodel import SiteListingView


def _view(vid: int, price: int, district: str) -> SiteListingView:
    return SiteListingView(
        id=vid, price_eur=price, area_m2=40.0, rooms="2-комн", floor=None, district=district,
        address=None, lat=None, lon=None, for_vnj=False, for_white_card=False, is_rented=False,
        photos=[], note=None, channel_url="https://t.me/RentBelgrad",
        posted_at="2026-06-20T12:00:00+00:00",
    )


# --------------------------------------------------------------- delivery


@pytest.mark.asyncio
async def test_telegram_digest_sends_text_to_chat_id(monkeypatch) -> None:
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "tg_bot_token", "123:abc")

    sent = []
    closed = {"n": 0}

    class FakeSession:
        async def close(self):
            closed["n"] += 1

    class FakeBot:
        def __init__(self, token):
            self.session = FakeSession()

        async def send_message(self, chat_id, text):
            sent.append({"chat_id": chat_id, "text": text})

    monkeypatch.setattr("src.telegram.subscription_delivery.Bot", FakeBot)

    from src.telegram.subscription_delivery import send_subscription_digest_telegram

    listings = [_view(1, 600, "Врачар"), _view(2, 800, "Земун")]
    await send_subscription_digest_telegram(555, listings, "https://x/unsub?token=tok")

    assert len(sent) == 1
    assert sent[0]["chat_id"] == 555
    assert "Врачар" in sent[0]["text"]
    assert "Земун" in sent[0]["text"]
    assert "https://x/unsub?token=tok" in sent[0]["text"]
    assert closed["n"] == 1  # session always closed


@pytest.mark.asyncio
async def test_telegram_digest_empty_is_noop(monkeypatch) -> None:
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "tg_bot_token", "123:abc")
    made = {"n": 0}

    class FakeBot:
        def __init__(self, token):
            made["n"] += 1

    monkeypatch.setattr("src.telegram.subscription_delivery.Bot", FakeBot)
    from src.telegram.subscription_delivery import send_subscription_digest_telegram

    await send_subscription_digest_telegram(555, [], "https://x/unsub")
    assert made["n"] == 0  # no listings → no Bot created, no send


# --------------------------------------------------------------- /start linking handler


@pytest.mark.asyncio
async def test_start_handler_links_chat_id_by_token(tmp_path) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.db.models import Base, Subscription
    from src.site import db as sitedb
    from src.telegram.subscription_bot import handle_start

    url = f"sqlite+aiosqlite:///{tmp_path / 'link.db'}"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            Subscription(
                id=1, criteria={}, channel="telegram", email=None, telegram_chat_id=None,
                token="abc123", is_active=False,
            )
        )
        await s.commit()
    await engine.dispose()

    replies = []

    class FakeMessage:
        text = "/start sub_abc123"

        class chat:
            id = 98765

        async def answer(self, text):
            replies.append(text)

    linked = await handle_start(FakeMessage(), database_url=url)
    assert linked is True
    assert len(replies) == 1  # a confirmation reply

    async with sitedb.read_session(database_url=url) as s:
        from sqlalchemy import select

        sub = (await s.execute(select(Subscription).where(Subscription.id == 1))).scalar_one()
        assert sub.telegram_chat_id == 98765
        assert sub.is_active is True


@pytest.mark.asyncio
async def test_start_handler_refuses_relink_to_a_different_chat(tmp_path) -> None:
    """Security (review fix): the token rides in every digest, so a leaked token must NOT let a
    different chat hijack an already-linked subscription. A re-link from another chat is refused;
    the original chat_id is untouched. (A re-link from the SAME chat stays idempotent.)"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.db.models import Base, Subscription
    from src.site import db as sitedb
    from src.telegram.subscription_bot import handle_start

    url = f"sqlite+aiosqlite:///{tmp_path / 'relink.db'}"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            Subscription(
                id=1, criteria={}, channel="telegram", email=None,
                telegram_chat_id=111,  # already linked to chat 111
                token="abc123", is_active=True,
            )
        )
        await s.commit()
    await engine.dispose()

    replies: list[str] = []

    class AttackerMessage:
        text = "/start sub_abc123"  # leaked token

        class chat:
            id = 999  # a DIFFERENT chat trying to hijack

        async def answer(self, text):
            replies.append(text)

    linked = await handle_start(AttackerMessage(), database_url=url)
    assert linked is False  # refused
    async with sitedb.read_session(database_url=url) as s:
        sub = (await s.execute(select(Subscription).where(Subscription.id == 1))).scalar_one()
        assert sub.telegram_chat_id == 111  # untouched — not hijacked

    # Same-chat re-link is still allowed (idempotent).
    class OwnerMessage(AttackerMessage):
        class chat:
            id = 111

    assert await handle_start(OwnerMessage(), database_url=url) is True
    async with sitedb.read_session(database_url=url) as s:
        sub = (await s.execute(select(Subscription).where(Subscription.id == 1))).scalar_one()
        assert sub.telegram_chat_id == 111


@pytest.mark.asyncio
async def test_start_handler_unknown_token_replies_but_links_nothing(tmp_path) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.db.models import Base
    from src.telegram.subscription_bot import handle_start

    url = f"sqlite+aiosqlite:///{tmp_path / 'link.db'}"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    replies = []

    class FakeMessage:
        text = "/start sub_nope"

        class chat:
            id = 1

        async def answer(self, text):
            replies.append(text)

    linked = await handle_start(FakeMessage(), database_url=url)
    assert linked is False
    assert len(replies) == 1  # still replies (graceful), but links nothing


@pytest.mark.asyncio
async def test_start_handler_plain_start_without_token_is_graceful(tmp_path) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.db.models import Base
    from src.telegram.subscription_bot import handle_start

    url = f"sqlite+aiosqlite:///{tmp_path / 'link.db'}"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    replies = []

    class FakeMessage:
        text = "/start"

        class chat:
            id = 1

        async def answer(self, text):
            replies.append(text)

    linked = await handle_start(FakeMessage(), database_url=url)
    assert linked is False
    assert len(replies) == 1
