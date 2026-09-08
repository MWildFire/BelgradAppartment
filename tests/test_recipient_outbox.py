"""F17: partial, concurrent and ambiguous sends preserve recipient-level truth."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.config import get_settings
from src.db import get_session
from src.db.models import MatchDelivery, PublishedPost
from src.telegram import bot_delivery
from src.telegram.delivery_ledger import (
    claim_recipient,
    prepare_recipients,
    reconcile_match_delivery,
)
from tests.test_auto_publish import _seed_match_with_real_photo
from tests.test_bot_media_contract import StrictBot


async def _states(mid):
    async with get_session() as s:
        rows = (await s.execute(select(MatchDelivery).where(MatchDelivery.match_id == mid))).scalars().all()
        return {row.recipient_id: row.status for row in rows}


@pytest.fixture
def bot_config(monkeypatch):
    monkeypatch.setattr(get_settings(), "tg_bot_recipients", "123,456")
    monkeypatch.setattr(get_settings(), "auto_publish_on_send", False)


@pytest.mark.asyncio
async def test_partial_success_retries_only_failed_recipient(db, tmp_path, bot_config, monkeypatch):
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    bot = StrictBot()
    fail = [True]
    async def send_photo(**kwargs):
        bot.photo_calls.append(kwargs)
        if kwargs["chat_id"] == 456 and fail[0]:
            raise ValueError("definite rejection")
        return SimpleNamespace(message_id=111)
    bot.send_photo = send_photo
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    await bot_delivery.send_match_via_bot(mid)
    assert await _states(mid) == {123: "sent", 456: "failed"}
    fail[0] = False
    result = await bot_delivery.retry_pending_match_deliveries()
    assert result["completed"] == 1
    assert [c["chat_id"] for c in bot.photo_calls] == [123, 456, 456]
    assert await _states(mid) == {123: "sent", 456: "sent"}


@pytest.mark.asyncio
async def test_timeout_after_acceptance_is_unknown_not_automatically_replayed(db, tmp_path, bot_config, monkeypatch):
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    bot = StrictBot()
    async def send_photo(**kwargs):
        bot.photo_calls.append(kwargs)
        if kwargs["chat_id"] == 456:
            raise TimeoutError("accepted then response lost")
        return SimpleNamespace(message_id=111)
    bot.send_photo = send_photo
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    await bot_delivery.send_match_via_bot(mid)
    await bot_delivery.retry_pending_match_deliveries()
    await bot_delivery.send_match_via_bot(mid)
    assert [c["chat_id"] for c in bot.photo_calls] == [123, 456]
    assert await _states(mid) == {123: "sent", 456: "unknown"}
    assert await reconcile_match_delivery(mid, 456, delivered_message_ids=[222])
    assert await _states(mid) == {123: "sent", 456: "sent"}


@pytest.mark.asyncio
async def test_atomic_claim_allows_only_one_concurrent_sender(db, tmp_path, bot_config):
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    await prepare_recipients(mid, [123])
    claims = await asyncio.gather(claim_recipient(mid, 123), claim_recipient(mid, 123))
    assert sum(token is not None for token in claims) == 1


@pytest.mark.asyncio
async def test_historical_published_post_never_becomes_verified_or_replayed(db, tmp_path, bot_config, monkeypatch):
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    async with get_session() as s:
        s.add(PublishedPost(match_id=mid, tg_message_id=101, post_text="historical"))
    bot = StrictBot()
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    result = await bot_delivery.send_match_via_bot(mid)
    assert result.post_text == "historical"
    assert await _states(mid) == {}
    assert bot.photo_calls == [] and bot.album_calls == []


@pytest.mark.asyncio
async def test_interrupted_claim_requires_reconciliation(db, tmp_path, bot_config):
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    await prepare_recipients(mid, [123])
    assert await claim_recipient(mid, 123)
    async with get_session() as s:
        row = (await s.execute(select(MatchDelivery))).scalar_one()
        row.claimed_at = datetime.now(UTC) - timedelta(hours=1)
    await prepare_recipients(mid, [123])
    assert await claim_recipient(mid, 123) is None
    assert await _states(mid) == {123: "unknown"}


@pytest.mark.asyncio
async def test_missing_media_never_records_success(db, tmp_path, bot_config, monkeypatch):
    from src.db.models import InboundPost
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    async with get_session() as s:
        inbound = (await s.execute(select(InboundPost))).scalar_one()
        inbound.photo_paths = [*inbound.photo_paths, str(tmp_path / "missing.jpg")]
    with pytest.raises(bot_delivery.BotDeliveryError, match="missing inbound photo"):
        await bot_delivery.send_match_via_bot(mid)
    assert await _states(mid) == {}


@pytest.mark.asyncio
async def test_full_concurrent_fanouts_send_each_recipient_once(db, tmp_path, bot_config, monkeypatch):
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    bot = StrictBot()
    original = bot.send_photo
    async def slow_send(**kwargs):
        await asyncio.sleep(0)
        return await original(**kwargs)
    bot.send_photo = slow_send
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    await asyncio.gather(bot_delivery.send_match_via_bot(mid), bot_delivery.send_match_via_bot(mid), return_exceptions=True)
    assert sorted(c["chat_id"] for c in bot.photo_calls) == [123, 456]
    assert await _states(mid) == {123: "sent", 456: "sent"}


@pytest.mark.asyncio
async def test_post_acceptance_database_failure_never_becomes_retryable(db, tmp_path, bot_config, monkeypatch):
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    bot = StrictBot()
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    original = bot_delivery.finish_recipient
    async def fail_commit(*args, **kwargs):
        if kwargs.get("message_ids"):
            raise RuntimeError("database commit unavailable after acceptance")
        return await original(*args, **kwargs)
    monkeypatch.setattr(bot_delivery, "finish_recipient", fail_commit)
    with pytest.raises(RuntimeError, match="database commit"):
        await bot_delivery.send_match_via_bot(mid)
    assert (await _states(mid))[123] == "sending"
    assert [c["chat_id"] for c in bot.photo_calls] == [123]


@pytest.mark.asyncio
async def test_force_cannot_bypass_unknown_reconciliation(db, tmp_path, bot_config):
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    await prepare_recipients(mid, [123])
    async with get_session() as session:
        row = (await session.execute(select(MatchDelivery))).scalar_one()
        row.status = "unknown"
    with pytest.raises(ValueError, match="reconcile unknown"):
        await prepare_recipients(mid, [123], force=True)
    assert await _states(mid) == {123: "unknown"}
