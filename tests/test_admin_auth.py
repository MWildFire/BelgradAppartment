"""Tests for admin basic-auth hardening: per-IP lockout + one-time default-password warning.

The lockout store is in-process and per-IP (mirrors src/site/deps.py's lead_rate_limiter).
We never sleep real time — the lockout uses an injectable monotonic clock (auth._now) that we
monkeypatch to fast-forward past the cooldown window.
"""

from __future__ import annotations

import base64
import logging

import pytest
from fastapi.testclient import TestClient

import src.admin.auth as auth
from src.admin.app import create_app


def _basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


GOOD = _basic("darya", "test_secret_123")
BAD = _basic("darya", "wrong")


@pytest.fixture(autouse=True)
def _force_admin_password(monkeypatch):
    """Deterministic credentials regardless of .env."""
    from src.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "admin_username", "darya")
    monkeypatch.setattr(s, "admin_password", "test_secret_123")
    yield


@pytest.fixture(autouse=True)
def _reset_lockout():
    """Each test starts with a clean, real-clock lockout store and un-warned default flag."""
    auth._admin_lockout.reset()
    auth._default_password_warned = False
    yield
    auth._admin_lockout.reset()
    auth._default_password_warned = False


def _client(host: str = "10.0.0.1") -> TestClient:
    """TestClient whose request.client.host is a controllable fake IP."""
    return TestClient(create_app(), client=(host, 12345))


def test_correct_password_on_fresh_ip_passes(db):
    client = _client("10.0.0.9")
    r = client.get("/queue", headers=GOOD)
    assert r.status_code == 200


def test_lockout_after_n_failures_blocks_even_correct_password():
    client = _client("10.0.0.2")
    # Exhaust the failure budget with wrong passwords.
    for _ in range(auth.LOCKOUT_MAX_FAILURES):
        r = client.get("/queue", headers=BAD)
        assert r.status_code == 401
    # Now even the *correct* password is refused with 429 during cooldown.
    r = client.get("/queue", headers=GOOD)
    assert r.status_code == 429
    assert r.headers.get("retry-after")
    assert int(r.headers["retry-after"]) > 0


def test_successful_auth_resets_failure_counter(db):
    client = _client("10.0.0.3")
    # One short of lockout...
    for _ in range(auth.LOCKOUT_MAX_FAILURES - 1):
        assert client.get("/queue", headers=BAD).status_code == 401
    # ...a success clears the counter...
    assert client.get("/queue", headers=GOOD).status_code == 200
    # ...so we can fail (MAX-1) more times again without being locked out.
    for _ in range(auth.LOCKOUT_MAX_FAILURES - 1):
        assert client.get("/queue", headers=BAD).status_code == 401
    # Still allowed because the counter was reset.
    assert client.get("/queue", headers=GOOD).status_code == 200


def test_lockout_expires_after_cooldown(db, monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(auth, "_now", lambda: t["now"])
    # Rebuild the store so it picks up the patched clock.
    auth._admin_lockout = auth._AdminLockout(clock=lambda: t["now"])

    client = _client("10.0.0.4")
    for _ in range(auth.LOCKOUT_MAX_FAILURES):
        assert client.get("/queue", headers=BAD).status_code == 401
    assert client.get("/queue", headers=GOOD).status_code == 429

    # Fast-forward just past the cooldown — lockout must have expired.
    t["now"] += auth.LOCKOUT_COOLDOWN_SECONDS + 1
    assert client.get("/queue", headers=GOOD).status_code == 200


def test_lockout_is_per_ip(db):
    """One IP being locked out must not affect a different IP."""
    locked = _client("10.0.0.5")
    for _ in range(auth.LOCKOUT_MAX_FAILURES):
        assert locked.get("/queue", headers=BAD).status_code == 401
    assert locked.get("/queue", headers=GOOD).status_code == 429

    other = _client("10.0.0.6")
    assert other.get("/queue", headers=GOOD).status_code == 200


def test_default_password_warning_logs_once(monkeypatch, caplog):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_password", "change_me")
    client = _client("10.0.0.7")
    creds = _basic("darya", "change_me")

    with caplog.at_level(logging.CRITICAL, logger=auth.logger.name):
        for _ in range(5):
            r = client.get("/queue", headers=creds)
            assert r.status_code == 401
    critical = [rec for rec in caplog.records if rec.levelno == logging.CRITICAL]
    assert len(critical) == 1
