"""F17: partial, concurrent and ambiguous sends preserve recipient-level truth."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import src.db.session as db_session
from src.config import get_settings
from src.db import get_session
from src.db.models import Base, MatchDelivery, MatchStatus, PublishedPost
from src.telegram import bot_delivery
from src.telegram.delivery_ledger import (
    claim_recipient,
    finish_recipient,
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


@pytest_asyncio.fixture
async def concurrent_db(tmp_path, monkeypatch):
    """Separate SQLite connections with the production WAL/timeout/FK settings.

    The shared :memory: fixture puts independent sessions on one connection; one
    session's rollback can undo another session's uncommitted delivery receipt.
    """
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_session_factory", None)
    engine = db_session.init_engine(f"sqlite+aiosqlite:///{tmp_path}/outbox.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await db_session.dispose_engine()


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
async def test_atomic_claim_allows_only_one_concurrent_sender(concurrent_db, tmp_path, bot_config):
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
async def test_full_concurrent_fanouts_send_each_recipient_once(concurrent_db, tmp_path, bot_config, monkeypatch):
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    bot = StrictBot()
    original = bot.send_photo
    async def slow_send(**kwargs):
        await asyncio.sleep(0)
        return await original(**kwargs)
    bot.send_photo = slow_send
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    results = await asyncio.gather(
        bot_delivery.send_match_via_bot(mid), bot_delivery.send_match_via_bot(mid),
    )
    assert sorted(c["chat_id"] for c in bot.photo_calls) == [123, 456]
    assert await _states(mid) == {123: "sent", 456: "sent"}
    assert len({result.id for result in results}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected_during_finalize", [False, True])
async def test_concurrent_finalizers_keep_one_summary_and_confirmed_receipts(
    concurrent_db, tmp_path, bot_config, monkeypatch, rejected_during_finalize,
):
    """Both workers observe no summary before writing; neither may lose its receipt."""
    from src.db.models import Match, SiteListing

    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    monkeypatch.setattr(get_settings(), "auto_publish_on_send", True)
    bot = StrictBot()
    send_barrier = asyncio.Barrier(2)

    async def concurrent_send(**kwargs):
        bot.photo_calls.append(kwargs)
        await send_barrier.wait()
        return SimpleNamespace(message_id=kwargs["chat_id"] + 1000)

    bot.send_photo = concurrent_send
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    receipt_barrier = asyncio.Barrier(2)
    original_finish = bot_delivery.finish_recipient

    async def commit_both_receipts(*args, **kwargs):
        await original_finish(*args, **kwargs)
        await receipt_barrier.wait()

    monkeypatch.setattr(bot_delivery, "finish_recipient", commit_both_receipts)
    finalize_barrier = asyncio.Barrier(2)
    rejection_committed = asyncio.Event()
    summary_reads = {}
    finalizer_connections = set()
    original_execute = AsyncSession.execute

    async def intersect_finalizers(session, statement, *args, **kwargs):
        result = await original_execute(session, statement, *args, **kwargs)
        if getattr(statement, "is_select", False) and any(
            item.get("entity") is PublishedPost for item in statement.column_descriptions
        ):
            task = asyncio.current_task()
            summary_reads[task] = summary_reads.get(task, 0) + 1
            if summary_reads[task] == 2:
                connection = await session.connection()
                raw = await connection.get_raw_connection()
                finalizer_connections.add(id(raw.driver_connection))
                last_reader = await finalize_barrier.wait()
                if rejected_during_finalize:
                    if last_reader == 0:
                        async with get_session() as operator:
                            await operator.execute(update(Match).where(Match.id == mid)
                                                   .values(status=MatchStatus.REJECTED))
                        rejection_committed.set()
                    await rejection_committed.wait()
        return result

    monkeypatch.setattr(AsyncSession, "execute", intersect_finalizers)
    async with asyncio.timeout(10):
        results = await asyncio.gather(
            bot_delivery.send_match_via_bot(mid), bot_delivery.send_match_via_bot(mid),
        )
    assert len(finalizer_connections) == 2
    assert sorted(call["chat_id"] for call in bot.photo_calls) == [123, 456]
    assert len({result.id for result in results}) == 1
    async with get_session() as session:
        rows = (await session.execute(select(MatchDelivery))).scalars().all()
        assert {row.recipient_id: (row.status, row.message_ids, row.attempts) for row in rows} == {
            123: ("sent", [1123], 1), 456: ("sent", [1456], 1),
        }
        assert all(row.claim_token is None and row.delivered_at is not None for row in rows)
        published = (await session.execute(select(PublishedPost))).scalar_one()
        assert published.tg_message_id == 1123
        assert published.post_text == bot.photo_calls[0]["caption"]
        status = MatchStatus.REJECTED if rejected_during_finalize else MatchStatus.PUBLISHED
        assert (await session.get(Match, mid)).status == status
        overlay = (await session.execute(select(SiteListing))).scalar_one_or_none()
        assert overlay is None if rejected_during_finalize else overlay.is_public
    # A subsequent automatic retry must not replay either confirmed recipient.
    assert (await bot_delivery.retry_pending_match_deliveries())["considered"] == 0
    assert len(bot.photo_calls) == 2


@pytest.mark.asyncio
async def test_summary_without_owned_claim_uses_the_confirmed_frozen_caption(
    db, tmp_path, bot_config, monkeypatch,
):
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    from src.db.models import InboundPost

    async with get_session() as session:
        paths = (await session.execute(select(InboundPost.photo_paths))).scalar_one()
    frozen = {"caption": "Даша: согласованный Finder", "photo_paths": paths}
    await prepare_recipients(mid, [123, 456], payload=frozen)
    for chat_id in [123, 456]:
        token = await claim_recipient(mid, chat_id)
        await finish_recipient(mid, chat_id, token, message_ids=[chat_id + 1000])
    bot = StrictBot()
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    published = await bot_delivery.send_match_via_bot(mid, post_text_override="Другой текст")
    assert published.post_text == frozen["caption"]
    assert published.tg_message_id == 1123
    assert bot.photo_calls == [] and bot.album_calls == []
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


@pytest.mark.asyncio
@pytest.mark.parametrize("all_fail", [False, True])
async def test_retry_reuses_original_override_caption_and_ordered_media(db, tmp_path, bot_config, monkeypatch, all_fail):
    from src.db.models import InboundPost, Listing
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    bot = StrictBot()
    fail = [True]
    async def send_photo(**kwargs):
        bot.photo_calls.append(kwargs)
        if fail[0] and (all_fail or kwargs["chat_id"] == 456):
            raise ValueError("definite rejection")
        return SimpleNamespace(message_id=777)
    bot.send_photo = send_photo
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    if all_fail:
        with pytest.raises(bot_delivery.BotDeliveryError):
            await bot_delivery.send_match_via_bot(mid, post_text_override="Даша: точный согласованный текст")
    else:
        await bot_delivery.send_match_via_bot(mid, post_text_override="Даша: точный согласованный текст")
    original_caption = bot.photo_calls[0]["caption"]
    original_path = bot.photo_calls[0]["photo"].path
    async with get_session() as session:
        listing = (await session.execute(select(Listing))).scalar_one()
        listing.price_eur = 9999
        inbound = (await session.execute(select(InboundPost))).scalar_one()
        inbound.photo_paths = [str(tmp_path / "unrelated-missing-new-photo.jpg")]
    fail[0] = False
    stats = await bot_delivery.retry_pending_match_deliveries()
    assert stats["completed"] == 1
    retry_calls = bot.photo_calls[2:]
    assert len(retry_calls) == (2 if all_fail else 1)
    assert all(call["caption"] == original_caption for call in retry_calls)
    assert all(call["photo"].path == original_path for call in retry_calls)


@pytest.mark.asyncio
async def test_incomplete_album_receipt_is_unknown_not_sent(db, tmp_path, bot_config, monkeypatch):
    from src.db.models import InboundPost
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    second = tmp_path / "second.jpg"
    await asyncio.to_thread(second.write_bytes, b"second-image")
    async with get_session() as session:
        inbound = (await session.execute(select(InboundPost))).scalar_one()
        inbound.photo_paths = [*inbound.photo_paths, str(second)]
    bot = StrictBot()
    async def partial_receipt(**kwargs):
        assert len(kwargs["media"]) == 2
        return [SimpleNamespace(message_id=111)]
    bot.send_media_group = partial_receipt
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    with pytest.raises(bot_delivery.BotDeliveryError):
        await bot_delivery.send_match_via_bot(mid)
    assert await _states(mid) == {123: "unknown", 456: "unknown"}
    async with get_session() as session:
        assert (await session.execute(select(PublishedPost))).scalar_one_or_none() is None
