"""F22: Bot API double enforces Telegram's actual album cardinality."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import FSInputFile, InputMediaPhoto

from src.telegram.bot_delivery import _send_album_with_floodwait


class StrictBot:
    def __init__(self):
        self.photo_calls = []
        self.album_calls = []
        self.session = SimpleNamespace(close=AsyncMock())

    async def send_photo(self, **kwargs):
        self.photo_calls.append(kwargs)
        return SimpleNamespace(message_id=101)

    async def send_media_group(self, **kwargs):
        assert 2 <= len(kwargs["media"]) <= 10, "Bot API rejects singleton albums"
        self.album_calls.append(kwargs)
        return [SimpleNamespace(message_id=101 + i) for i in range(len(kwargs["media"]))]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 10])
async def test_photo_or_album_preserves_caption_and_order(count):
    bot = StrictBot()
    media = [InputMediaPhoto(media=FSInputFile(f"{i}.jpg"), caption="unchanged caption" if i == 0 else None) for i in range(count)]
    result = await _send_album_with_floodwait(bot, 123, media)
    assert len(result) == count
    if count == 1:
        assert bot.album_calls == []
        assert bot.photo_calls[0]["caption"] == "unchanged caption"
        assert bot.photo_calls[0]["photo"].path == "0.jpg"
    else:
        assert bot.photo_calls == []
        assert [p.media.path for p in bot.album_calls[0]["media"]] == [f"{i}.jpg" for i in range(count)]
        assert bot.album_calls[0]["media"][0].caption == "unchanged caption"


@pytest.mark.asyncio
async def test_real_send_uses_immutable_render_finder_caption_and_media(db, monkeypatch, nekretnine_detail_html):
    from src.config import get_settings
    from src.db import get_session
    from src.db.models import InboundPost, Match, SourceChannel
    from src.scrapers.nekretnine import NekretnineScraper
    from src.telegram import bot_delivery
    from tests.test_render_finder_zero_regression import ROOT, _listing_from_detail, _manifest

    oracle = _manifest()
    expected = oracle["expected_render"]
    detail = NekretnineScraper.parse_detail(oracle["independent_inputs"]["legacy_detail"]["url"], nekretnine_detail_html)
    media_paths = [str(ROOT / item["path"]) for item in oracle["raw_fixture_hashes"]["inbound_media"]]
    async with get_session() as session:
        session.add(SourceChannel(id=1, tg_id=-1, username="golden-probe"))
        listing = _listing_from_detail(detail)
        inbound = InboundPost(source_channel_id=1, tg_message_id=1, raw_text="350€ 50m² Свободна с 1 сентября", photo_paths=media_paths)  # noqa: RUF001 - immutable Russian oracle
        session.add_all([listing, inbound])
        await session.flush()
        match = Match(listing_id=listing.id, inbound_post_id=inbound.id, confidence=1.0, matched_photo_count=2, method="phash")
        session.add(match)
        await session.flush()
        match_id = match.id
    bot = StrictBot()
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    monkeypatch.setattr(get_settings(), "tg_bot_recipients", "123,456")
    monkeypatch.setattr(get_settings(), "auto_publish_on_send", False)
    await bot_delivery.send_match_via_bot(match_id)
    assert len(bot.album_calls) == 2
    for call in bot.album_calls:
        assert call["media"][0].caption == expected["full_caption"]
        assert [item.media.path for item in call["media"]] == media_paths
