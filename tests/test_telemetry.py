"""Tests for src.telegram.telemetry — fail-safe no_match notifications."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
from src.db.models import Base, InboundPost, InboundStatus, SourceChannel
from src.matching.matcher import METHOD_CLIP, METHOD_CLIP_LLM_REJECTED


@pytest_asyncio.fixture
async def db(tmp_path: Path, monkeypatch):
    """In-memory DB + tmp data_dir."""
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    # default-enable for most tests
    monkeypatch.setattr(settings, "notify_no_match_enabled", True)
    monkeypatch.setattr(settings, "tg_api_id", 12345)
    monkeypatch.setattr(settings, "tg_api_hash", "abc")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    orig_engine = db_session._engine
    orig_factory = db_session._session_factory
    db_session._engine = engine
    db_session._session_factory = factory
    try:
        yield factory
    finally:
        await engine.dispose()
        db_session._engine = orig_engine
        db_session._session_factory = orig_factory


async def _seed_inbound(factory, **overrides) -> int:
    """Insert a SourceChannel + InboundPost and return inbound id."""
    async with factory() as s:
        ch = SourceChannel(tg_id=-1001, username="OtzRentBelgrad", is_active=True)
        s.add(ch)
        await s.flush()
        defaults = dict(
            source_channel_id=ch.id,
            tg_message_id=13561,
            raw_text="#Voždovac\n50м² 700€ #до1000",
            photo_paths=["a.jpg"],
            photo_phashes=["0" * 16],
            parsed_price_eur=700,
            parsed_area_m2=50.0,
            status=InboundStatus.NO_MATCH,
        )
        defaults.update(overrides)
        ib = InboundPost(**defaults)
        s.add(ib)
        await s.commit()
        return ib.id


def _patch_telethon_client():
    """Return a patcher for TelegramClient that captures send_message calls."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.send_message = AsyncMock()

    factory = MagicMock(return_value=mock_client)
    return patch("src.telegram.telemetry.TelegramClient", new=factory), mock_client


@pytest.mark.asyncio
async def test_notify_sends_message_in_normal_case(db):
    """Happy path: flag on, creds present, inbound exists → message lands in Saved."""
    from src.telegram.telemetry import _do_notify_no_match

    iid = await _seed_inbound(db)
    patcher, mock_client = _patch_telethon_client()
    with patcher:
        await _do_notify_no_match(
            iid, best_count=1, threshold=2, method=METHOD_CLIP, confidence=0.83
        )

    mock_client.send_message.assert_awaited_once()
    target, body = mock_client.send_message.await_args.args
    assert target == "me"
    assert f"inbound #{iid}" in body
    assert "no_match" in body
    assert "1/2" in body
    assert "700€" in body
    assert "50м²" in body
    assert "https://t.me/OtzRentBelgrad/13561" in body
    assert "ниже порога photo-match" in body


@pytest.mark.asyncio
async def test_notify_llm_rejected_uses_distinct_reason(db):
    """method=clip+llm_rejected → reason text differs from below-threshold case."""
    from src.telegram.telemetry import _do_notify_no_match

    iid = await _seed_inbound(db)
    patcher, mock_client = _patch_telethon_client()
    with patcher:
        await _do_notify_no_match(
            iid, best_count=4, threshold=2, method=METHOD_CLIP_LLM_REJECTED, confidence=0.91
        )

    _, body = mock_client.send_message.await_args.args
    assert "LLM отклонил кандидата" in body
    assert "ниже порога" not in body


@pytest.mark.asyncio
async def test_notify_disabled_via_flag_short_circuits(db):
    """notify_no_match_enabled=False → no Telethon connection at all."""
    from src.config import get_settings
    from src.telegram.telemetry import _do_notify_no_match

    iid = await _seed_inbound(db)
    get_settings().notify_no_match_enabled = False
    try:
        patcher, mock_client = _patch_telethon_client()
        with patcher as factory:
            await _do_notify_no_match(
                iid, best_count=1, threshold=2, method=METHOD_CLIP, confidence=0.5
            )
        factory.assert_not_called()
        mock_client.send_message.assert_not_awaited()
    finally:
        get_settings().notify_no_match_enabled = True


@pytest.mark.asyncio
async def test_notify_missing_credentials_short_circuits(db):
    """No TG_API_ID → emit one warning, then silently skip."""
    from src.config import get_settings
    from src.telegram.telemetry import _do_notify_no_match

    iid = await _seed_inbound(db)
    get_settings().tg_api_id = 0
    # reset module flag so we can observe the warning fire
    import src.telegram.telemetry as tele
    tele._creds_warning_emitted = False

    patcher, mock_client = _patch_telethon_client()
    with patcher as factory:
        await _do_notify_no_match(
            iid, best_count=1, threshold=2, method=METHOD_CLIP, confidence=0.5
        )
        await _do_notify_no_match(
            iid, best_count=1, threshold=2, method=METHOD_CLIP, confidence=0.5
        )
    factory.assert_not_called()
    mock_client.send_message.assert_not_awaited()
    assert tele._creds_warning_emitted is True


@pytest.mark.asyncio
async def test_notify_swallows_telethon_errors(db, caplog):
    """If Telethon raises mid-send, the exception is logged and never propagated."""
    import logging

    from src.telegram.telemetry import _do_notify_no_match

    iid = await _seed_inbound(db)
    boom = AsyncMock()
    boom.__aenter__ = AsyncMock(side_effect=RuntimeError("network down"))
    boom.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=boom)
    with caplog.at_level(logging.ERROR), patch("src.telegram.telemetry.TelegramClient", new=factory):
        await _do_notify_no_match(
            iid, best_count=1, threshold=2, method=METHOD_CLIP, confidence=0.5
        )
    assert any("failed to notify no_match" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_notify_missing_inbound_returns_silently(db):
    """If the inbound row vanished between match and notify, just return."""
    from src.telegram.telemetry import _do_notify_no_match

    patcher, mock_client = _patch_telethon_client()
    with patcher as factory:
        await _do_notify_no_match(
            999_999, best_count=1, threshold=2, method=METHOD_CLIP, confidence=0.5
        )
    factory.assert_not_called()
    mock_client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_no_match_returns_a_task(db):
    """Public entry point is fire-and-forget — returns a Task, doesn't block on send."""
    from src.telegram.telemetry import notify_no_match

    iid = await _seed_inbound(db)
    patcher, _ = _patch_telethon_client()
    with patcher:
        task = notify_no_match(
            iid, best_count=1, threshold=2, method=METHOD_CLIP, confidence=0.5
        )
        assert isinstance(task, asyncio.Task)
        await task  # let it finish so pytest doesn't warn about pending tasks


@pytest.mark.asyncio
async def test_notify_handles_post_without_text(db):
    """raw_text='' → uses placeholder, doesn't crash on splitlines()[0]."""
    from src.telegram.telemetry import _do_notify_no_match

    iid = await _seed_inbound(db, raw_text="", parsed_price_eur=None, parsed_area_m2=None)
    patcher, mock_client = _patch_telethon_client()
    with patcher:
        await _do_notify_no_match(
            iid, best_count=0, threshold=2, method=METHOD_CLIP, confidence=0.0
        )

    _, body = mock_client.send_message.await_args.args
    assert "(без текста)" in body
