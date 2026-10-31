"""One poller per token: `otz subscription-bot` and `otz bot` run the same unified daemon.

Routers follow the flags (SUBSCRIPTION_BOT_ENABLED -> /start links, WATERMARK_BOT_ENABLED ->
/clean); with every flag off the daemon logs a warning and exits without touching Telegram.
"""

from __future__ import annotations

import logging

import aiogram
import pytest
from click.testing import CliRunner

from src.cli import cli
from src.config import get_settings
from src.telegram import bot_daemon, subscription_bot
from src.watermark.settings import get_watermark_settings


@pytest.fixture
def flags(monkeypatch):
    """Set the two feature flags and the shared bot credentials in one place."""

    def apply(*, wm=False, sub=False, token="", recipients="", chat_ids="", site_clean=False):
        s, w = get_settings(), get_watermark_settings()
        monkeypatch.setattr(w, "watermark_bot_enabled", wm)
        monkeypatch.setattr(w, "watermark_allowed_chat_ids", chat_ids)
        monkeypatch.setattr(w, "watermark_site_use_clean", site_clean)
        monkeypatch.setattr(s, "subscription_bot_enabled", sub)
        monkeypatch.setattr(s, "tg_bot_token", token)
        monkeypatch.setattr(s, "tg_bot_recipients", recipients)

    return apply


@pytest.fixture
def telegram(monkeypatch):
    """Fake aiogram Bot + Dispatcher: records what the daemon does, never opens a socket."""
    calls: dict[str, object] = {"bots": 0, "polling": 0}

    class FakeSession:
        async def close(self):
            calls["closed"] = True

    class FakeBot:
        def __init__(self, token):
            calls["bots"] = int(calls["bots"]) + 1  # type: ignore[call-overload]
            calls["token"] = token
            self.session = FakeSession()

        async def delete_webhook(self, **kwargs):
            calls["delete_webhook"] = kwargs

    class FakeDispatcher:
        def resolve_used_update_types(self):
            return ["message"]

        async def start_polling(self, bot, **kwargs):
            calls["polling"] = int(calls["polling"]) + 1  # type: ignore[call-overload]
            calls["allowed_updates"] = kwargs.get("allowed_updates")

    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    monkeypatch.setattr(bot_daemon, "build_dispatcher", lambda: FakeDispatcher())
    return calls


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
def test_dispatcher_routers_follow_flags(flags, wm, sub, recipients, expected):
    flags(wm=wm, sub=sub, recipients=recipients)
    dp = bot_daemon.build_dispatcher()
    assert [r.name for r in dp.sub_routers] == expected


async def test_daemon_noops_when_everything_disabled(flags, telegram, caplog):
    flags()
    with caplog.at_level(logging.WARNING):
        assert await bot_daemon.run_bot_daemon() is None
    assert telegram["bots"] == 0 and telegram["polling"] == 0
    assert "nothing enabled" in caplog.text


async def test_daemon_requires_token_when_enabled(flags):
    flags(wm=True, token="")
    with pytest.raises(RuntimeError):
        await bot_daemon.run_bot_daemon()


async def test_daemon_keeps_queued_updates(flags, telegram):
    """/start links waiting in the Telegram queue survive a restart (old photos: wm_bot skips)."""
    flags(wm=True, token="42:TEST")
    await bot_daemon.run_bot_daemon()
    assert telegram["delete_webhook"] == {"drop_pending_updates": False}
    assert telegram["polling"] == 1 and telegram["closed"] is True


async def test_subscriptions_alone_still_poll_once(flags, telegram):
    """SUBSCRIPTION_BOT_ENABLED=1 and /clean off: exactly the old `subscription-bot` behaviour."""
    flags(sub=True, token="42:TEST")
    await bot_daemon.run_bot_daemon()
    assert telegram["token"] == "42:TEST"
    assert telegram["bots"] == 1 and telegram["polling"] == 1


async def test_subscription_function_runs_the_single_daemon(monkeypatch):
    started = {"n": 0}

    async def fake_daemon():
        started["n"] += 1

    monkeypatch.setattr(bot_daemon, "run_bot_daemon", fake_daemon)
    await subscription_bot.run_subscription_bot()
    assert started["n"] == 1


@pytest.mark.parametrize("command", ["subscription-bot", "bot"])
def test_both_cli_names_start_the_same_daemon(monkeypatch, command):
    started = {"n": 0}

    async def fake_daemon():
        started["n"] += 1

    monkeypatch.setattr(bot_daemon, "run_bot_daemon", fake_daemon)
    result = CliRunner().invoke(cli, [command])
    assert result.exit_code == 0, result.output
    assert started["n"] == 1


@pytest.mark.parametrize("command", ["subscription-bot", "bot"])
def test_cli_with_both_flags_off_exits_without_polling_telegram(flags, telegram, caplog, command):
    flags()
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(cli, [command])
    assert result.exit_code == 0, result.output
    assert telegram["bots"] == 0 and telegram["polling"] == 0
    assert "nothing enabled" in caplog.text


@pytest.mark.parametrize("command", ["subscription-bot", "bot"])
def test_cli_with_subscriptions_on_polls_once(flags, telegram, command):
    flags(sub=True, token="42:TEST")
    result = CliRunner().invoke(cli, [command])
    assert result.exit_code == 0, result.output
    assert telegram["bots"] == 1 and telegram["polling"] == 1
