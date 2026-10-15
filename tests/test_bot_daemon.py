"""One poller: routers follow the flags; with every flag off the daemon does nothing."""

from __future__ import annotations

import logging

import aiogram
import pytest

from src.config import get_settings
from src.telegram import bot_daemon, subscription_bot


@pytest.mark.parametrize(
    ("wm", "sub", "recipients", "expected"),
    [
        (False, False, "1", []),
        (True, False, "1,2", ["watermark"]),
        (True, False, "", []),
        (False, True, "", ["subscription"]),
        (True, True, "5", ["watermark", "subscription"]),
    ],
)
def test_dispatcher_routers_follow_flags(monkeypatch, wm, sub, recipients, expected):
    s = get_settings()
    monkeypatch.setattr(s, "watermark_bot_enabled", wm)
    monkeypatch.setattr(s, "subscription_bot_enabled", sub)
    monkeypatch.setattr(s, "tg_bot_recipients", recipients)
    monkeypatch.setattr(s, "watermark_allowed_chat_ids", "")
    dp = bot_daemon.build_dispatcher()
    assert [r.name for r in dp.sub_routers] == expected


async def test_daemon_noops_when_everything_disabled(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "watermark_bot_enabled", False)
    monkeypatch.setattr(s, "subscription_bot_enabled", False)
    monkeypatch.setattr(s, "tg_bot_token", "")
    assert await bot_daemon.run_bot_daemon() is None


async def test_daemon_requires_token_when_enabled(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "watermark_bot_enabled", True)
    monkeypatch.setattr(s, "tg_bot_token", "")
    with pytest.raises(RuntimeError):
        await bot_daemon.run_bot_daemon()


async def test_daemon_keeps_queued_updates(monkeypatch):
    """/start links waiting in the Telegram queue survive a restart (old photos: wm_bot skips)."""
    calls: dict[str, object] = {}

    class FakeSession:
        async def close(self):
            calls["closed"] = True

    class FakeBot:
        def __init__(self, token):
            calls["token"] = token
            self.session = FakeSession()

        async def delete_webhook(self, **kwargs):
            calls["delete_webhook"] = kwargs

    class FakeDispatcher:
        def resolve_used_update_types(self):
            return []

        async def start_polling(self, bot, **_kwargs):
            calls["polling"] = True

    s = get_settings()
    monkeypatch.setattr(s, "watermark_bot_enabled", True)
    monkeypatch.setattr(s, "subscription_bot_enabled", False)
    monkeypatch.setattr(s, "tg_bot_token", "42:TEST")
    monkeypatch.setattr(s, "watermark_site_use_clean", False)
    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    monkeypatch.setattr(bot_daemon, "build_dispatcher", lambda: FakeDispatcher())
    await bot_daemon.run_bot_daemon()
    assert calls["delete_webhook"] == {"drop_pending_updates": False}
    assert calls["polling"] is True and calls["closed"] is True


async def test_old_subscription_command_never_starts_a_second_poller(monkeypatch, caplog):
    started = {"n": 0}

    async def fake_daemon():
        started["n"] += 1

    monkeypatch.setattr(bot_daemon, "run_bot_daemon", fake_daemon)
    monkeypatch.setattr(get_settings(), "subscription_bot_enabled", True)
    with caplog.at_level(logging.WARNING):
        await subscription_bot.run_subscription_bot()
    assert started["n"] == 0
    assert "replaced by otz bot (otz-bot.service), not starting" in caplog.text
