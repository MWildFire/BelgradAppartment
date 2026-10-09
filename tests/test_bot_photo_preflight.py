"""Bot photo limits are checked locally without altering or uploading the original media."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from src.config import get_settings
from src.db import get_session
from src.db.models import InboundPost, Match, MatchDelivery, MatchStatus, PublishedPost
from src.telegram import bot_delivery
from src.telegram.delivery_ledger import prepare_recipients
from tests.test_auto_publish import _seed_match_with_real_photo
from tests.test_bot_media_contract import StrictBot

TEN_MB = 10 * 1024 * 1024


@pytest.fixture
def bot(monkeypatch):
    double = StrictBot()
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: double)
    monkeypatch.setattr(get_settings(), "tg_bot_recipients", "123,456")
    monkeypatch.setattr(get_settings(), "auto_publish_on_send", False)
    return double


async def _album(tmp_path, sizes):
    mid = await _seed_match_with_real_photo(get_session, tmp_path)
    paths = []
    for index, size in enumerate(sizes):
        path = tmp_path / f"photo-{index}.png"
        with path.open("wb") as stream:
            stream.write(b"synthetic-size-fixture")
            stream.truncate(size)
        paths.append(path)
    async with get_session() as session:
        inbound = (await session.execute(select(InboundPost))).scalar_one()
        inbound.photo_paths = [str(path) for path in paths]
    return mid, paths


@pytest.mark.parametrize("sizes", [[TEN_MB + 1], [32, TEN_MB + 1]])
async def test_oversized_photo_is_known_failure_and_retry_never_calls_http(
    db, tmp_path, bot, sizes, caplog
):
    mid, paths = await _album(tmp_path, sizes)
    original = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    with pytest.raises(
        bot_delivery.BotDeliveryError, match=r"10 MB.*no upload attempted"
    ) as caught:
        await bot_delivery.send_match_via_bot(mid)
    assert type(caught.value).__name__ == "BotPhotoTooLargeError"
    assert f"photo {len(paths)}" in str(caught.value)
    assert str(TEN_MB + 1) in str(caught.value)
    assert bot.photo_calls == bot.album_calls == []
    async with get_session() as session:
        rows = (await session.execute(select(MatchDelivery))).scalars().all()
        assert {row.recipient_id: (row.status, row.error_code) for row in rows} == {
            123: ("failed", "BotPhotoTooLargeError"),
            456: ("failed", "BotPhotoTooLargeError"),
        }
        assert (await session.get(Match, mid)).status == MatchStatus.PENDING
        assert (await session.execute(select(PublishedPost))).scalar_one_or_none() is None
    stats = await bot_delivery.retry_pending_match_deliveries()
    assert stats == {"considered": 1, "completed": 0, "errors": 1, "unknown": 0}
    assert bot.photo_calls == bot.album_calls == []
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths] == original
    assert "BotPhotoTooLargeError" in caplog.text
    assert "no upload attempted" in caplog.text
    assert str(tmp_path) not in caplog.text


@pytest.mark.parametrize("size", [TEN_MB - 1, TEN_MB])
async def test_valid_boundary_preserves_caption_order_and_bytes(db, tmp_path, bot, size):
    mid, paths = await _album(tmp_path, [32, size])
    original = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    await bot_delivery.send_match_via_bot(mid, post_text_override="Exact approved caption")
    assert bot.photo_calls == []
    assert [call["chat_id"] for call in bot.album_calls] == [123, 456]
    for call in bot.album_calls:
        assert [item.media.path for item in call["media"]] == [str(path) for path in paths]
        assert call["media"][0].caption.endswith("Exact approved caption")
        assert call["media"][1].caption is None
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths] == original


async def test_oversized_unselected_eleventh_photo_does_not_change_first_ten(db, tmp_path, bot):
    mid, paths = await _album(tmp_path, [32] * 10 + [TEN_MB + 1])
    await bot_delivery.send_match_via_bot(mid)
    assert len(bot.album_calls) == 2
    assert all(
        [item.media.path for item in call["media"]] == [str(path) for path in paths[:10]]
        for call in bot.album_calls
    )


async def test_preflight_checks_frozen_retry_payload_not_current_inbound(db, tmp_path, bot):
    mid, paths = await _album(tmp_path, [32])
    frozen = tmp_path / "original-too-large.png"
    with frozen.open("wb") as stream:
        stream.truncate(TEN_MB + 1)
    await prepare_recipients(
        mid,
        [123, 456],
        payload={
            "caption": "Original caption",
            "photo_paths": [str(frozen)],
        },
    )
    with pytest.raises(bot_delivery.BotDeliveryError, match="no upload attempted"):
        await bot_delivery.send_match_via_bot(mid)
    assert bot.photo_calls == bot.album_calls == []
    assert paths[0].stat().st_size == 32


async def test_historical_post_is_not_replayed_or_reclassified_by_preflight(db, tmp_path, bot):
    mid, _ = await _album(tmp_path, [TEN_MB + 1])
    async with get_session() as session:
        session.add(PublishedPost(match_id=mid, tg_message_id=101, post_text="Historical caption"))
    result = await bot_delivery.send_match_via_bot(mid)
    assert result.post_text == "Historical caption"
    assert bot.photo_calls == bot.album_calls == []
    async with get_session() as session:
        assert (await session.execute(select(MatchDelivery))).scalars().all() == []
