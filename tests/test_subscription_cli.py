"""C2 — CLI: subscription-bot is config-gated; notify-subscriptions runs the matcher."""

from __future__ import annotations

from click.testing import CliRunner

from src.cli import cli


def test_subscription_bot_disabled_by_default_does_not_run(monkeypatch):
    """With every bot flag off (default) the command exits at once and never polls Telegram."""
    import aiogram

    from src.config import get_settings
    from src.watermark.settings import get_watermark_settings

    monkeypatch.setattr(get_settings(), "subscription_bot_enabled", False)
    monkeypatch.setattr(get_watermark_settings(), "watermark_bot_enabled", False)

    def explode(*_args, **_kwargs):
        raise AssertionError("the daemon must not create a Telegram bot when everything is off")

    monkeypatch.setattr(aiogram, "Bot", explode)
    runner = CliRunner()
    result = runner.invoke(cli, ["subscription-bot"])
    assert result.exit_code == 0


def test_notify_subscriptions_command_invokes_matcher(monkeypatch):
    import src.jobs.notify_subscriptions as notify_mod

    captured = {}

    async def fake_notify(*, since_hours=24, dry_run=False, database_url=None):
        captured["since_hours"] = since_hours
        captured["dry_run"] = dry_run
        return notify_mod.NotifyStats(subscriptions_notified=1, listings_sent=3)

    monkeypatch.setattr(notify_mod, "notify_subscriptions", fake_notify)
    runner = CliRunner()
    result = runner.invoke(cli, ["notify-subscriptions", "--dry-run", "--since-hours", "48"])
    assert result.exit_code == 0
    assert captured == {"since_hours": 48, "dry_run": True}
    assert "1" in result.output  # reports notified count
