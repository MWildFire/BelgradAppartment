"""C2 — the notify_subscriptions nightly step: summary line, flag gating, fault isolation."""

from __future__ import annotations

import pytest

import src.jobs.notify_subscriptions as notify_mod
from src.cli import _run_nightly_steps
from src.config import get_settings


@pytest.fixture(autouse=True)
def _disable_other_steps(monkeypatch):
    """Run ONLY the subscriptions step in isolation."""
    settings = get_settings()
    monkeypatch.setattr(settings, "nightly_heal_limit", 0)
    monkeypatch.setattr(settings, "nightly_detail_refresh", 0)
    monkeypatch.setattr(settings, "nightly_verify_links", False)
    monkeypatch.setattr(settings, "nightly_auto_publish", False)
    monkeypatch.setattr(settings, "nightly_metrics", False)
    monkeypatch.setattr(settings, "nightly_prune_embeddings", False)
    yield


@pytest.mark.asyncio
async def test_subscriptions_step_adds_summary_line(monkeypatch):
    monkeypatch.setattr(get_settings(), "nightly_notify_subscriptions", True)

    async def fake_notify(*, since_hours=24, dry_run=False, database_url=None):
        return notify_mod.NotifyStats(
            subscriptions_considered=4, subscriptions_notified=2, listings_sent=5, errors=0
        )

    monkeypatch.setattr(notify_mod, "notify_subscriptions", fake_notify)
    lines, _pc, _alerts, _hidden, _alarm = await _run_nightly_steps()
    assert any("подписки" in ln and "2" in ln for ln in lines), lines


@pytest.mark.asyncio
async def test_subscriptions_step_disabled_by_flag(monkeypatch):
    monkeypatch.setattr(get_settings(), "nightly_notify_subscriptions", False)
    called = {"n": 0}

    async def fake_notify(*, since_hours=24, dry_run=False, database_url=None):
        called["n"] += 1
        return notify_mod.NotifyStats()

    monkeypatch.setattr(notify_mod, "notify_subscriptions", fake_notify)
    lines, _pc, _alerts, _hidden, _alarm = await _run_nightly_steps()
    assert called["n"] == 0
    assert not any("подписки" in ln for ln in lines)


@pytest.mark.asyncio
async def test_subscriptions_step_failure_is_isolated(monkeypatch):
    monkeypatch.setattr(get_settings(), "nightly_notify_subscriptions", True)

    async def boom(*, since_hours=24, dry_run=False, database_url=None):
        raise RuntimeError("digest blew up")

    monkeypatch.setattr(notify_mod, "notify_subscriptions", boom)
    # Must not raise — a failed step adds a 🚨 line, never crashes the nightly.
    lines, _pc, _alerts, _hidden, _alarm = await _run_nightly_steps()
    assert any("🚨" in ln and "подписк" in ln for ln in lines), lines


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
