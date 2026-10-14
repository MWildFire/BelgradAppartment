"""One poller: routers follow the flags; with every flag off the daemon does nothing."""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.telegram import bot_daemon


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
