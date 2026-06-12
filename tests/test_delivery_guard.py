"""Post-send status guard: a successful send must not resurrect a human REJECTED.

No existing delivery-transport tests to extend (test_pipeline mocks delivery wholesale), and
faking Telethon/aiogram just for one status line is disproportionate — so the guard lives in
the pure helper `resolve_post_send_status` (src/db/models.py) and is unit-tested here.
"""

from __future__ import annotations

import inspect

from src.db.models import MatchStatus, resolve_post_send_status


def test_pending_becomes_published():
    assert resolve_post_send_status(MatchStatus.PENDING) is MatchStatus.PUBLISHED


def test_rejected_mid_send_stays_rejected():
    # admin said no while the message was in flight — the human decision wins
    assert resolve_post_send_status(MatchStatus.REJECTED) is MatchStatus.REJECTED


def test_published_stays_published():
    # force_resend on an already-delivered match is a no-op transition
    assert resolve_post_send_status(MatchStatus.PUBLISHED) is MatchStatus.PUBLISHED


def test_both_send_paths_use_the_guard():
    # wiring check: neither module may regress to a bare `= MatchStatus.PUBLISHED`
    from src.telegram import bot_delivery, delivery

    assert "resolve_post_send_status" in inspect.getsource(delivery._send_match_via_userbot)
    assert "resolve_post_send_status" in inspect.getsource(bot_delivery.send_match_via_bot)
