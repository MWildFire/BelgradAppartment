"""WATERMARK_CLEAN_ON_DELIVERY: the first album goes out clean; on failure, the originals."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import select

from src.config import get_settings
from src.db import get_session
from src.db.models import InboundPost, Listing, Match, MatchDelivery, Platform, SourceChannel
from src.telegram import bot_delivery
from src.watermark import store
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background


class AlbumBot:
    def __init__(self):
        self.albums: list[list[str]] = []
        self.session = SimpleNamespace(close=AsyncMock())

    async def send_media_group(self, chat_id, media):
        self.albums.append([m.media.path for m in media])
        return [SimpleNamespace(message_id=700 + i) for i in range(len(media))]

    async def send_photo(self, chat_id, photo, caption=None):
        self.albums.append([photo.path])
        return SimpleNamespace(message_id=700)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    s = get_settings()
    inbound = tmp_path / "inbound"
    monkeypatch.setattr(s, "inbound_photos_dir", inbound)
    monkeypatch.setattr(s, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(s, "watermark_lama_enabled", False)
    monkeypatch.setattr(s, "tg_bot_recipients", "555")
    monkeypatch.setattr(s, "auto_publish_on_send", False)
    bot = AlbumBot()
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    return inbound, bot


async def _seed(inbound: Path) -> tuple[int, list[str]]:
    tpl = {t.id: t for t in load_templates()}["zida_logo"]
    paths = []
    for i in range(2):
        p = inbound / "1" / "88" / f"{i:02d}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(apply_mark(make_background(1440, 1080, seed=20 + i), tpl)).save(p, "JPEG", quality=92)
        paths.append(str(p))
    async with get_session() as s:
        s.add(SourceChannel(id=1, tg_id=-100, username="src"))
        listing = Listing(platform=Platform.CETIRI_ZIDA, external_id="z9", url="https://www.4zida.rs/y")
        inbound_post = InboundPost(source_channel_id=1, tg_message_id=88, raw_text="500€ 50m²", photo_paths=paths)
        s.add_all([listing, inbound_post])
        await s.flush()
        match = Match(listing_id=listing.id, inbound_post_id=inbound_post.id, confidence=1.0,
                      matched_photo_count=2, method="phash")
        s.add(match)
        await s.flush()
        return match.id, paths


async def _frozen_paths(match_id: int) -> list[str]:
    async with get_session() as s:
        payload = (await s.execute(select(MatchDelivery.payload).where(MatchDelivery.match_id == match_id))).scalar_one()
    return payload["photo_paths"]


async def test_flag_off_sends_originals(db, env):
    inbound, bot = env
    match_id, paths = await _seed(inbound)
    await bot_delivery.send_match_via_bot(match_id)
    assert bot.albums == [paths]


def _all_under(paths: list[str], root: Path) -> bool:
    return all(Path(p).resolve().is_relative_to(root.resolve()) for p in paths)


def _all_exist(paths: list[str]) -> bool:
    return all(Path(p).is_file() for p in paths)


async def test_flag_on_sends_clean_copies(db, env, monkeypatch):
    inbound, bot = env
    monkeypatch.setattr(get_settings(), "watermark_clean_on_delivery", True)
    match_id, paths = await _seed(inbound)
    await bot_delivery.send_match_via_bot(match_id)
    assert _all_under(bot.albums[0], Path(get_settings().watermark_clean_dir))
    assert await _frozen_paths(match_id) == paths
    assert _all_exist(paths)  # originals are still there


async def test_cleaning_failure_falls_back_to_originals(db, env, monkeypatch):
    inbound, bot = env
    monkeypatch.setattr(get_settings(), "watermark_clean_on_delivery", True)

    def boom(_stored, **_kw):
        raise RuntimeError("cv2 exploded")

    monkeypatch.setattr(store, "ensure_clean", boom)
    match_id, paths = await _seed(inbound)
    await bot_delivery.send_match_via_bot(match_id)
    assert bot.albums == [paths]


def _sizes(paths: list[str]) -> list[int]:
    return [Path(p).stat().st_size for p in paths]


async def test_clean_copy_over_the_bot_limit_falls_back_to_the_original(db, env, monkeypatch):
    inbound, bot = env
    monkeypatch.setattr(get_settings(), "watermark_clean_on_delivery", True)
    match_id, paths = await _seed(inbound)
    clean = [str(store.ensure_clean(p).best) for p in paths]  # type: ignore[union-attr]
    limit = min(_sizes(clean)) - 1
    assert max(_sizes(paths)) <= limit  # q95 clean copies are larger than the q92 originals
    monkeypatch.setattr(bot_delivery, "BOT_PHOTO_LIMIT_BYTES", limit)
    await bot_delivery.send_match_via_bot(match_id)
    assert bot.albums == [paths]


async def test_cleaning_timeout_sends_originals(db, env, monkeypatch):
    inbound, bot = env
    monkeypatch.setattr(get_settings(), "watermark_clean_on_delivery", True)
    monkeypatch.setattr(bot_delivery, "DELIVERY_CLEAN_TIMEOUT_SECONDS", 0.05)
    real = store.ensure_clean

    def slow(stored, **kw):
        time.sleep(1)
        return real(stored, **kw)

    monkeypatch.setattr(store, "ensure_clean", slow)
    match_id, paths = await _seed(inbound)
    await bot_delivery.send_match_via_bot(match_id)
    assert bot.albums == [paths]
