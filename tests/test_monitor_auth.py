"""Non-interactive auth gate for the monitor (production-incident fix).

A revoked/missing Telethon session must NEVER fall through to start()'s phone prompt on a
systemd unit. Instead:
- connect_authorized() raises SessionNotAuthorized when is_user_authorized() is False;
- the `monitor` command turns that into SystemExit(3) and attempts ONE bot alert.

All Telethon + bot interaction is mocked — no network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.telegram.monitor as monitor
from src.cli import _monitor
from src.telegram.monitor import SessionNotAuthorized, connect_authorized


@pytest.mark.asyncio
async def test_connect_authorized_raises_when_not_authorized():
    client = MagicMock()
    client.connect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=False)

    with pytest.raises(SessionNotAuthorized):
        await connect_authorized(client)

    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_authorized_ok_when_authorized():
    client = MagicMock()
    client.connect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)

    # No raise.
    await connect_authorized(client)
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_monitor_startup_gate_exits_3_and_alerts(monkeypatch):
    """Dead session at startup → SystemExit(3) + exactly one bot alert attempt."""
    from datetime import datetime, timezone

    from src.config import get_settings
    from src.db.models import SourceChannel
    from tests.conftest import fresh_db

    s = get_settings()
    monkeypatch.setattr(s, "tg_bot_token", "fake:token")

    # connect_authorized raises as if the session were revoked.
    async def _raise_unauth(_client):
        raise SessionNotAuthorized("dead")

    fake_client = MagicMock()
    fake_client.disconnect = AsyncMock()

    sent: list[str] = []

    async def _fake_send_text_via_bot(text: str) -> None:
        sent.append(text)

    async with fresh_db() as (_engine, factory):
        # One active source so we get past the no-sources early return.
        async with factory() as sess:
            sess.add(
                SourceChannel(
                    tg_id=-1001234500000,
                    username="src",
                    title="src",
                    is_active=True,
                    start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
                )
            )
            await sess.commit()

        with (
            patch.object(monitor, "make_client", return_value=fake_client),
            patch.object(monitor, "connect_authorized", _raise_unauth),
            patch(
                "src.telegram.bot_delivery.send_text_via_bot",
                new=_fake_send_text_via_bot,
            ),
        ):
            with pytest.raises(SystemExit) as exc:
                await _monitor(do_backfill_first=False)

    assert exc.value.code == 3
    assert len(sent) == 1
    assert "auth_qr" in sent[0]


@pytest.mark.asyncio
async def test_alert_auth_lost_noop_without_token(monkeypatch):
    """No bot token → no send attempt, no crash (just a logged error)."""
    from src.cli import _alert_auth_lost
    from src.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "tg_bot_token", "")

    with patch("src.telegram.bot_delivery.send_text_via_bot") as mock_send:
        await _alert_auth_lost()

    mock_send.assert_not_called()
