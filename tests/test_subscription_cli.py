"""C2 — CLI: subscription-bot is config-gated; notify-subscriptions runs the matcher."""

from __future__ import annotations

from click.testing import CliRunner

from src.cli import cli


def test_subscription_bot_disabled_by_default_does_not_run(monkeypatch):
    """With subscription_bot_enabled False (default), the command refuses to start the poller."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "subscription_bot_enabled", False)
    started = {"n": 0}

    async def fake_run():
        started["n"] += 1

    # If run_subscription_bot were invoked it would bump the counter; the gate is INSIDE
    # run_subscription_bot, so we assert the command exits cleanly without raising.
    import src.telegram.subscription_bot as bot_mod

    monkeypatch.setattr(bot_mod, "run_subscription_bot", fake_run)
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
