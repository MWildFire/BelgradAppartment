"""Offline scenarios of the single bot daemon: real Dispatcher + real routers, fake Telegram.

No network at all: a fake aiogram session records every API call, and socket.connect is a
tripwire. The cleaning engine is scripted (tests/fixtures/wm_n08_reports.json), so no photo is
decoded and no model is loaded. WM05-W3, B1 (texts) and the "who is served" policy.
"""
# ruff: noqa: RUF001  (Russian user-facing texts are asserted verbatim)

from __future__ import annotations

import asyncio
import socket
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import aiogram
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, File, Message, PhotoSize, Update, User

from src.config import get_settings
from src.telegram import bot_daemon, wm_bot
from src.watermark import pipeline
from src.watermark.settings import get_watermark_settings
from tests.test_wm_bot_results import ScriptedEngine

ALLOWED_CHAT = 5
KEPT_SENTENCE = "Знак найден и оставлен без изменений"


class FakeTelegram(BaseSession):
    """Records sends; serves 'downloads' as RAW<file_id>. Anything else is a test failure."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}  # file_id -> bytes served instead of RAW<file_id>

    async def close(self) -> None:
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        name = method.__api_method__
        if name == "getMe":
            return User(id=42, is_bot=True, first_name="Offline", username="offline_bot")
        if name == "getFile":
            return File(
                file_id=method.file_id, file_unique_id="u", file_path=f"files/{method.file_id}"
            )
        assert name in {"sendMessage", "sendPhoto", "sendDocument", "sendMediaGroup"}, name
        message = Message(
            message_id=9000 + len(self.calls), date=datetime.now(UTC),
            chat={"id": method.chat_id, "type": "private"},
        )
        if name == "sendMediaGroup":
            media = method.media
            self.calls.append({
                "method": name,
                "caption": media[0].caption,
                "files": [m.media.data for m in media],
            })
            return [message for _ in media]
        payload = getattr(method, "photo", None) or getattr(method, "document", None)
        self.calls.append({
            "method": name,
            "text": getattr(method, "text", None),
            "caption": getattr(method, "caption", None),
            "files": [payload.data] if payload is not None else [],
        })
        return message

    async def stream_content(self, url, *args, **kwargs):
        file_id = url.rsplit("/", 1)[-1]
        yield self.files.get(file_id, b"RAW" + file_id.encode())


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Tripwire for EVERY test of this module: nothing may open a network connection."""

    def no_network(*_args, **_kwargs):
        raise AssertionError("network transport is forbidden in offline scenarios")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)


@pytest.fixture
def daemon(monkeypatch):
    """(dispatcher, bot, fake telegram) of the unified daemon with /clean on for chat 5."""
    settings, wm_settings = get_settings(), get_watermark_settings()
    monkeypatch.setattr(wm_settings, "watermark_bot_enabled", True)
    monkeypatch.setattr(settings, "subscription_bot_enabled", False)
    monkeypatch.setattr(settings, "tg_bot_recipients", str(ALLOWED_CHAT))
    monkeypatch.setattr(wm_settings, "watermark_allowed_chat_ids", "")
    monkeypatch.setattr(wm_settings, "watermark_site_use_clean", False)
    # AlbumCollector captured its 1.5 s wait at definition time: shorten it for the tests.
    monkeypatch.setattr(wm_bot.AlbumCollector.__init__, "__defaults__", (0.02,))
    session = FakeTelegram()
    return bot_daemon.build_dispatcher(), Bot(token="42:TEST", session=session), session


def _photo_message(
    update_id: int, *, chat_id: int = ALLOWED_CHAT, chat_type: str = "private",
    group: str | None = None, age: timedelta = timedelta(0), caption: str | None = None,
) -> Update:
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC) - age,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=chat_id, is_bot=False, first_name="D"),
        photo=[PhotoSize(file_id=f"f{update_id}", file_unique_id="u", width=10, height=10)],
        media_group_id=group,
        caption=caption,
    )
    return Update(update_id=update_id, message=message)


def _text_message(
    text: str, *, chat_id: int = ALLOWED_CHAT, chat_type: str = "private",
    age: timedelta = timedelta(0),
):
    message = Message(
        message_id=1,
        date=datetime.now(UTC) - age,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=chat_id, is_bot=False, first_name="D"),
        text=text,
    )
    return Update(update_id=1, message=message)


async def _settle() -> None:
    await asyncio.sleep(0.3)  # album flush + handler tasks


def _engine(monkeypatch, *script: str) -> ScriptedEngine:
    engine = ScriptedEngine(*script)
    monkeypatch.setattr(pipeline, "clean_bytes", engine)
    return engine


async def test_album_of_ten_is_answered_once_with_one_caption(daemon, monkeypatch):
    dp, bot, session = daemon
    _engine(monkeypatch, "mixed_zida_043", "only_kept_halo_000", *["no_marks"] * 7, "boom")
    for i in range(10):
        await dp.feed_update(bot, _photo_message(100 + i, group="g1"))
    await _settle()
    assert [c["method"] for c in session.calls] == ["sendMessage", "sendMediaGroup"]
    assert session.calls[0]["text"] == "🧽 Обрабатываю 10 фото…"
    album = session.calls[1]
    assert len(album["files"]) == 10 and album["files"][9] == b"RAWf109"  # failed -> original
    caption = album["caption"]
    assert "Удалено: 4zida — 2; оставлено: Halo Oglasi — 2" in caption
    assert "без найденных знаков: 7 из 10 фото" in caption
    assert "Не удалось обработать: 1 из 10" in caption


async def test_single_photo_with_removal_comes_back_cleaned(daemon, monkeypatch):
    dp, bot, session = daemon
    _engine(monkeypatch, "removed_zida_001")
    await dp.feed_update(bot, _photo_message(200))
    await _settle()
    assert [c["method"] for c in session.calls] == ["sendMessage", "sendPhoto"]
    assert session.calls[1]["files"] == [b"CLEAN-RAWf200"]
    assert "Удалено: 4zida — 2" in session.calls[1]["caption"]


async def test_only_kept_is_text_only_and_says_kept(daemon, monkeypatch):
    dp, bot, session = daemon
    _engine(monkeypatch, "only_kept_halo_000")
    await dp.feed_update(bot, _photo_message(300))
    await _settle()
    assert [c["method"] for c in session.calls] == ["sendMessage", "sendMessage"]
    assert session.calls[1]["text"].startswith(KEPT_SENTENCE)


async def test_mixed_photo_reports_removed_and_kept(daemon, monkeypatch):
    dp, bot, session = daemon
    _engine(monkeypatch, "mixed_zida_043")
    await dp.feed_update(bot, _photo_message(400))
    await _settle()
    assert "Удалено: 4zida — 2; оставлено: Halo Oglasi — 1" in session.calls[-1]["caption"]


async def test_no_marks_is_told_plainly(daemon, monkeypatch):
    dp, bot, session = daemon
    _engine(monkeypatch, "no_marks")
    await dp.feed_update(bot, _photo_message(500))
    await _settle()
    assert session.calls[-1]["text"] == wm_bot.NOTHING_FOUND


async def test_engine_error_returns_original_with_honest_caption(daemon, monkeypatch):
    dp, bot, session = daemon
    _engine(monkeypatch, "boom")
    await dp.feed_update(bot, _photo_message(600))
    await _settle()
    assert session.calls[-1]["method"] == "sendPhoto"
    assert session.calls[-1]["files"] == [b"RAWf600"]
    assert session.calls[-1]["caption"].startswith("Не удалось обработать: 1 из 1")


@pytest.mark.parametrize(
    ("chat_id", "chat_type"), [(6, "private"), (ALLOWED_CHAT, "group"), (-100, "supergroup")]
)
async def test_other_chats_get_no_answer_at_all(daemon, monkeypatch, chat_id, chat_type):
    dp, bot, session = daemon
    engine = _engine(monkeypatch, "removed_zida_001")
    await dp.feed_update(bot, _photo_message(700, chat_id=chat_id, chat_type=chat_type))
    await dp.feed_update(bot, _text_message("/clean", chat_id=chat_id, chat_type=chat_type))
    await _settle()
    assert session.calls == [] and engine.calls == 0


async def test_clean_without_reply_shows_help_in_allowed_chat(daemon):
    dp, bot, session = daemon
    await dp.feed_update(bot, _text_message("/clean"))
    assert [c["text"] for c in session.calls] == [wm_bot.HELP_TEXT]


async def test_current_policy_old_photos_are_skipped_but_old_commands_are_not(daemon, monkeypatch):
    """Characterization only (policy unchanged): MAX_MEDIA_AGE applies to photos, not /clean."""
    dp, bot, session = daemon
    engine = _engine(monkeypatch, "removed_zida_001")
    await dp.feed_update(bot, _photo_message(800, age=timedelta(minutes=31)))
    await _settle()
    assert session.calls == [] and engine.calls == 0
    await dp.feed_update(bot, _text_message("/clean", age=timedelta(days=3)))
    assert [c["text"] for c in session.calls] == [wm_bot.HELP_TEXT]


async def test_both_flags_off_the_daemon_never_polls_telegram(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(get_watermark_settings(), "watermark_bot_enabled", False)
    monkeypatch.setattr(settings, "subscription_bot_enabled", False)
    monkeypatch.setattr(settings, "tg_bot_token", "42:TEST")

    def tripwire(*_args, **_kwargs):
        raise AssertionError("Telegram must not be contacted when every flag is off")

    monkeypatch.setattr(aiogram, "Bot", tripwire)
    monkeypatch.setattr(aiogram.Dispatcher, "start_polling", tripwire)
    monkeypatch.setattr(socket.socket, "connect", tripwire)
    assert await bot_daemon.run_bot_daemon() is None


async def test_clean_is_not_served_when_only_subscriptions_are_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(get_watermark_settings(), "watermark_bot_enabled", False)
    monkeypatch.setattr(settings, "subscription_bot_enabled", True)
    monkeypatch.setattr(settings, "tg_bot_recipients", str(ALLOWED_CHAT))
    session = FakeTelegram()
    dp = bot_daemon.build_dispatcher()
    await dp.feed_update(Bot(token="42:TEST", session=session), _text_message("/clean"))
    assert session.calls == [] and [r.name for r in dp.sub_routers] == ["subscription"]


# ---- both features on: /clean must not take the subscription links away (ANSWERS A1) ----------


@pytest.fixture
def daemon_both(monkeypatch):
    """Dispatcher with /clean (chat 5 allowed) AND subscriptions; /start is recorded."""
    from src.telegram import subscription_bot

    settings = get_settings()
    monkeypatch.setattr(get_watermark_settings(), "watermark_bot_enabled", True)
    monkeypatch.setattr(get_watermark_settings(), "watermark_allowed_chat_ids", "")
    monkeypatch.setattr(get_watermark_settings(), "watermark_site_use_clean", False)
    monkeypatch.setattr(settings, "subscription_bot_enabled", True)
    monkeypatch.setattr(settings, "tg_bot_recipients", str(ALLOWED_CHAT))
    started: list[tuple[int, str]] = []

    async def fake_handle_start(message, **_kw):
        started.append((message.chat.id, message.text))
        return True

    monkeypatch.setattr(subscription_bot, "handle_start", fake_handle_start)
    monkeypatch.setattr(wm_bot.AlbumCollector.__init__, "__defaults__", (0.02,))
    session = FakeTelegram()
    return bot_daemon.build_dispatcher(), Bot(token="42:TEST", session=session), session, started


@pytest.mark.parametrize("chat_id", [6, ALLOWED_CHAT])
async def test_start_with_a_subscription_token_reaches_handle_start_from_any_chat(
    daemon_both, chat_id
):
    dp, bot, session, started = daemon_both
    await dp.feed_update(bot, _text_message("/start sub_abc123", chat_id=chat_id))
    assert started == [(chat_id, "/start sub_abc123")]
    assert session.calls == []  # the /clean router did not answer it


@pytest.mark.parametrize("chat_id", [6, ALLOWED_CHAT])
async def test_plain_start_still_reaches_the_subscription_handler(daemon_both, chat_id):
    dp, bot, _session, started = daemon_both
    await dp.feed_update(bot, _text_message("/start", chat_id=chat_id))
    assert started == [(chat_id, "/start")]


async def test_clean_and_photos_from_a_foreign_chat_are_not_processed_with_both_on(
    daemon_both, monkeypatch
):
    dp, bot, session, started = daemon_both
    engine = _engine(monkeypatch, "removed_zida_001")
    await dp.feed_update(bot, _text_message("/clean", chat_id=6))
    await dp.feed_update(bot, _photo_message(900, chat_id=6))
    await _settle()
    assert session.calls == [] and engine.calls == 0 and started == []


async def test_clean_still_works_in_the_allowed_chat_with_both_on(daemon_both, monkeypatch):
    dp, bot, session, started = daemon_both
    _engine(monkeypatch, "removed_zida_001")
    await dp.feed_update(bot, _photo_message(901))
    await _settle()
    assert session.calls[-1]["method"] == "sendPhoto" and started == []


# ---- LaMa worker failure seen from the chat: honest text, daemon keeps serving --------------


def _halo_center_jpeg(seed: int = 2) -> bytes:
    import io

    from PIL import Image

    from src.watermark.templates import load_templates
    from tests.watermark.synth import apply_mark, make_background

    tpl = {t.id: t for t in load_templates()}["halo_center"]
    buf = io.BytesIO()
    # Portrait frame: its LaMa mask (4 %) is under the 5.5 % self-check cap, so the mark reaches
    # LaMa; a 4:3 landscape frame (7 %) is kept as self_check before the worker is asked.
    Image.fromarray(apply_mark(make_background(800, 1000, seed=seed), tpl)).save(
        buf, "JPEG", quality=92
    )
    return buf.getvalue()


def _fake_worker(handler: str):
    code = (
        "import os, numpy as np\nfrom src.watermark.lama_proc import serve\n"
        f"serve({handler})\n"
    )
    return lambda _model, _threads: [sys.executable, "-c", code]


async def test_lama_worker_failure_is_told_honestly_and_the_next_photo_still_works(
    daemon, monkeypatch, tmp_path
):
    from src.watermark import lama, lama_proc

    dp, bot, session = daemon
    session.files = {"f1000": _halo_center_jpeg(2), "f1001": _halo_center_jpeg(3)}
    model = tmp_path / "big-lama.pt"
    model.write_bytes(b"fake")
    wm_settings = get_watermark_settings()
    monkeypatch.setattr(wm_settings, "watermark_lama_enabled", True)
    monkeypatch.setattr(wm_settings, "watermark_lama_model_path", model)
    monkeypatch.setattr(wm_settings, "watermark_lama_isolation", "process")
    monkeypatch.setattr(wm_settings, "watermark_lama_timeout_seconds", 20)
    monkeypatch.setattr(lama, "torch_available", lambda: True)
    lama.reset_lama_inpainter()
    try:  # the real engine runs; only the LaMa worker is a fake (first it dies, then it works)
        monkeypatch.setattr(
            lama_proc, "build_worker_command", _fake_worker("lambda o, i, m: os.kill(os.getpid(), 9)")
        )
        await dp.feed_update(bot, _photo_message(1000))
        await _settle()
        assert session.calls[-1]["method"] == "sendMessage"
        assert session.calls[-1]["text"].startswith(KEPT_SENTENCE)
        assert not any(c["method"] == "sendPhoto" for c in session.calls)

        lama.reset_lama_inpainter()
        monkeypatch.setattr(
            lama_proc, "build_worker_command",
            _fake_worker("lambda o, i, m: np.full(i.shape, (7, 7, 7), np.uint8)"),
        )
        await dp.feed_update(bot, _photo_message(1001))
        await _settle()
        assert session.calls[-1]["method"] == "sendPhoto"
        assert "Удалено: Halo Oglasi — 1" in session.calls[-1]["caption"]
    finally:
        lama.reset_lama_inpainter()


async def test_clean_on_but_nobody_allowed_and_no_subscriptions_means_nothing_to_serve(
    monkeypatch, caplog
):
    """Review L4/F7: no router -> do not poll Telegram (a poller would eat the updates)."""
    settings = get_settings()
    wm_settings = get_watermark_settings()
    monkeypatch.setattr(wm_settings, "watermark_bot_enabled", True)
    monkeypatch.setattr(wm_settings, "watermark_allowed_chat_ids", "")
    monkeypatch.setattr(wm_settings, "watermark_site_use_clean", False)
    monkeypatch.setattr(settings, "tg_bot_recipients", "")
    monkeypatch.setattr(settings, "subscription_bot_enabled", False)
    monkeypatch.setattr(settings, "tg_bot_token", "42:TEST")

    def tripwire(*_args, **_kwargs):
        raise AssertionError("Telegram must not be contacted when there is nothing to serve")

    monkeypatch.setattr(aiogram, "Bot", tripwire)
    monkeypatch.setattr(aiogram.Dispatcher, "start_polling", tripwire)
    # Alembic migration tests disable existing loggers through fileConfig; restore this one.
    monkeypatch.setattr(bot_daemon.logger, "disabled", False)
    with caplog.at_level("WARNING"):
        assert await bot_daemon.run_bot_daemon() is None
    assert "nothing to serve" in caplog.text
