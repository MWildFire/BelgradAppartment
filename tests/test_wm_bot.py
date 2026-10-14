"""RentFinderBot /clean: fake messages and bot, no network (like test_bot_media_contract.py)."""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, PhotoSize, Update, User
from PIL import Image

from src.config import get_settings
from src.db import get_session
from src.db.models import InboundPost, Listing, Match, MatchDelivery, Platform, SourceChannel
from src.telegram import wm_bot
from src.watermark.pipeline import decode_image, find_marks
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background

CHAT = 1003207518


def _jpeg(marked: bool, seed: int = 1) -> bytes:
    img = make_background(1440, 1080, seed=seed)
    if marked:
        img = apply_mark(img, {t.id: t for t in load_templates()}["zida_logo"])
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "JPEG", quality=92)
    return buf.getvalue()


class FakeBot:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    async def download(self, file_obj):
        return io.BytesIO(self.files[file_obj.file_id])


class FakeMessage:
    def __init__(self, message_id, *, bot=None, photo_id=None, document=None, group=None, reply=None):
        self.message_id = message_id
        self.chat = SimpleNamespace(id=CHAT, type="private")
        self.bot = bot
        self.photo = [SimpleNamespace(file_id=photo_id)] if photo_id else None
        self.document = document
        self.media_group_id = group
        self.reply_to_message = reply
        self.texts: list[str] = []
        self.photos: list[tuple[bytes, str | None]] = []
        self.documents: list[tuple[bytes, str | None]] = []
        self.albums: list[list] = []

    async def answer(self, text, **_kw):
        self.texts.append(text)

    async def answer_photo(self, photo, caption=None, **_kw):
        self.photos.append((photo.data, caption))

    async def answer_document(self, document, caption=None, **_kw):
        self.documents.append((document.data, caption))

    async def answer_media_group(self, media, **_kw):
        self.albums.append(media)


@pytest.fixture(autouse=True)
def _settings(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "watermark_lama_enabled", False)
    monkeypatch.setattr(s, "inbound_photos_dir", tmp_path / "inbound")
    monkeypatch.setattr(s, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(s, "watermark_site_use_clean", False)


def test_parse_chat_ids_ignores_garbage():
    assert wm_bot.parse_chat_ids("1, 2", "x,3,", "") == frozenset({1, 2, 3})


async def test_single_photo_comes_back_clean():
    bot = FakeBot({"f1": _jpeg(marked=True)})
    msg = FakeMessage(10, bot=bot, photo_id="f1")
    await wm_bot.process_photo_messages([msg])
    assert len(msg.photos) == 1
    data, caption = msg.photos[0]
    assert "4zida — 2" in (caption or "")
    assert find_marks(decode_image(data)) == []


async def test_photo_without_marks_is_reported():
    bot = FakeBot({"f1": _jpeg(marked=False)})
    msg = FakeMessage(11, bot=bot, photo_id="f1")
    await wm_bot.process_photo_messages([msg])
    assert msg.photos == [] and msg.texts[-1] == wm_bot.NOTHING_FOUND


async def test_image_document_comes_back_as_document():
    bot = FakeBot({"d1": _jpeg(marked=True)})
    doc = SimpleNamespace(file_id="d1", mime_type="image/jpeg", file_size=200_000, file_name="flat.jpg")
    msg = FakeMessage(12, bot=bot, document=doc)
    await wm_bot.process_photo_messages([msg])
    assert len(msg.documents) == 1


async def test_album_is_collected_into_one_reply():
    bot = FakeBot({"a": _jpeg(True, 1), "b": _jpeg(True, 2)})
    handled: list[list] = []

    async def handler(messages):
        handled.append(messages)

    collector = wm_bot.AlbumCollector(handler, wait=0.05)
    await collector.add(FakeMessage(21, bot=bot, photo_id="a", group="g1"))
    await collector.add(FakeMessage(22, bot=bot, photo_id="b", group="g1"))
    await asyncio.sleep(0.2)
    assert [[m.message_id for m in batch] for batch in handled] == [[21, 22]]


async def test_album_handler_errors_are_logged_not_raised(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(
        wm_bot.logger, "exception", lambda msg, *args, **_kw: logged.append(msg % args)
    )

    async def boom(_messages):
        raise RuntimeError("telegram is down")

    collector = wm_bot.AlbumCollector(boom, wait=0.01)
    await collector.add(FakeMessage(60, photo_id="x", group="g9"))
    await asyncio.sleep(0.1)
    assert logged == [f"[wm-bot] album handler failed chat={CHAT}"]


async def test_clean_without_reply_shows_help():
    msg = FakeMessage(30)
    await wm_bot.handle_clean_command(msg, None)
    assert msg.texts == [wm_bot.HELP_TEXT]


def _all_exist(paths: list[str]) -> bool:
    return all(Path(p).is_file() for p in paths)


async def _seed_delivery(tmp_path: Path) -> tuple[int, list[str]]:
    inbound = get_settings().inbound_photos_dir
    paths = []
    for i in range(2):
        p = Path(inbound) / "1" / "77" / f"{i:02d}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_jpeg(marked=True, seed=10 + i))
        paths.append(str(p))
    async with get_session() as s:
        s.add(SourceChannel(id=1, tg_id=-100, username="src"))
        listing = Listing(platform=Platform.CETIRI_ZIDA, external_id="z1", url="https://www.4zida.rs/x")
        inbound_post = InboundPost(source_channel_id=1, tg_message_id=77, raw_text="", photo_paths=paths)
        s.add_all([listing, inbound_post])
        await s.flush()
        match = Match(listing_id=listing.id, inbound_post_id=inbound_post.id, confidence=1.0,
                      matched_photo_count=2, method="phash")
        s.add(match)
        await s.flush()
        s.add(MatchDelivery(match_id=match.id, recipient_id=CHAT, status="sent", message_ids=[501, 502],
                            payload={"caption": "c", "photo_paths": paths}))
        return match.id, paths


async def test_reply_to_delivered_album_cleans_that_match(db, tmp_path):
    match_id, paths = await _seed_delivery(tmp_path)
    msg = FakeMessage(40, reply=SimpleNamespace(message_id=502, photo=None, document=None))
    await wm_bot.handle_clean_command(msg, None)
    assert len(msg.albums) == 1 and len(msg.albums[0]) == 2
    assert "4zida — 4" in msg.albums[0][0].caption
    clean_copy = Path(get_settings().watermark_clean_dir) / "1" / "77" / "00.jpg"
    assert _all_exist([str(clean_copy), *paths])  # clean copy written, originals kept
    again = FakeMessage(41)
    await wm_bot.handle_clean_command(again, str(match_id))
    assert len(again.albums) == 1


async def test_reply_to_unknown_message(db):
    msg = FakeMessage(50, reply=SimpleNamespace(message_id=999, photo=None, document=None))
    await wm_bot.handle_clean_command(msg, None)
    assert msg.texts == [wm_bot.NOT_FOUND_MATCH]


async def test_unknown_match_id(db):
    msg = FakeMessage(51)
    await wm_bot.handle_clean_command(msg, "424242")
    assert msg.texts == [wm_bot.MATCH_NOT_FOUND.format(match_id=424242)]


async def test_clean_in_photo_caption_goes_to_the_photo_handler(monkeypatch):
    calls: list[str] = []

    async def fake_add(self, message):
        calls.append("photo")

    async def fake_clean(message, args):
        calls.append("clean")

    monkeypatch.setattr(wm_bot.AlbumCollector, "add", fake_add)
    monkeypatch.setattr(wm_bot, "handle_clean_command", fake_clean)
    dp = Dispatcher()
    dp.include_router(wm_bot.build_watermark_router(frozenset({5})))
    bot = Bot(token="42:TEST")
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=5, type="private"),
        from_user=User(id=5, is_bot=False, first_name="D"),
        photo=[PhotoSize(file_id="f", file_unique_id="u", width=10, height=10)],
        caption="/clean",
    )
    await dp.feed_update(bot, Update(update_id=1, message=msg))
    await bot.session.close()
    assert calls == ["photo"]


def test_is_stale_only_for_photos_older_than_thirty_minutes():
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    assert wm_bot.is_stale(SimpleNamespace(date=now - timedelta(minutes=31)), now=now)
    assert not wm_bot.is_stale(SimpleNamespace(date=now - timedelta(minutes=29)), now=now)
    assert not wm_bot.is_stale(SimpleNamespace(date=None), now=now)
