"""De-dupe for the systemd OnFailure notifier (production-incident fix).

OnFailure fires on every transition to `failed`, so a residual N-transition storm could send
N alerts. `_notify_failure` writes a per-service marker under /var/log/otz and suppresses sends
within a cooldown — so the storm collapses to one message.

The bot/userbot send is mocked (no network); the marker dir is pointed at tmp_path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import src.cli as cli


@pytest.fixture
def marker_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_NOTIFY_MARKER_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_notify_failure_dedupes_within_cooldown(marker_dir):
    """Two calls for the same service within cooldown → only ONE actual send."""
    sends: list[str] = []

    async def _fake_send(_client, text: str) -> None:
        sends.append(text)

    with (
        patch("src.telegram.delivery.send_text_to_recipients", new=_fake_send),
        patch.object(cli, "_open_userbot_if_needed", AsyncMock(return_value=None)),
    ):
        await cli._notify_failure("otz-monitor", None)
        await cli._notify_failure("otz-monitor", None)

    assert len(sends) == 1
    # Marker was written so the second call could see it.
    assert (marker_dir / ".last_alert_otz-monitor").exists()


@pytest.mark.asyncio
async def test_notify_failure_distinct_services_not_suppressed(marker_dir):
    """Different services have independent markers → both send."""
    sends: list[str] = []

    async def _fake_send(_client, text: str) -> None:
        sends.append(text)

    with (
        patch("src.telegram.delivery.send_text_to_recipients", new=_fake_send),
        patch.object(cli, "_open_userbot_if_needed", AsyncMock(return_value=None)),
    ):
        await cli._notify_failure("otz-monitor", None)
        await cli._notify_failure("otz-nightly-refresh.service", None)

    assert len(sends) == 2


@pytest.mark.asyncio
async def test_notify_failure_not_marked_when_send_fails(marker_dir):
    """If the send itself fails, do NOT write the marker — the next attempt must retry."""

    async def _boom(_client, _text: str) -> None:
        raise RuntimeError("delivery down")

    with (
        patch("src.telegram.delivery.send_text_to_recipients", new=_boom),
        patch.object(cli, "_open_userbot_if_needed", AsyncMock(return_value=None)),
    ):
        await cli._notify_failure("otz-monitor", None)

    assert not (marker_dir / ".last_alert_otz-monitor").exists()


@pytest.mark.asyncio
async def test_notify_failure_monitor_hint_mentions_reauth(marker_dir):
    """The monitor alert recovery hint must mention QR re-auth, not just reset-failed/start."""
    sends: list[str] = []

    async def _fake_send(_client, text: str) -> None:
        sends.append(text)

    with (
        patch("src.telegram.delivery.send_text_to_recipients", new=_fake_send),
        patch.object(cli, "_open_userbot_if_needed", AsyncMock(return_value=None)),
    ):
        await cli._notify_failure("otz-monitor", "session revoked")

    assert len(sends) == 1
    body = sends[0]
    assert "auth_qr" in body
    # No longer asserts the false "restarts exhausted" claim.
    assert "авторестартов больше не будет" not in body
