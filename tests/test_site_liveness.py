"""Tests for the liveness sweep + its config.

Liveness replaces the age-based 21-day retention notice (D5): a public card stays up while the
apartment is still listed on ≥1 platform (its A4 dup-group) and only comes down when confirmed gone
from ALL. This file is the successor to the old tests/test_retention.py.
"""

from __future__ import annotations

from src.config import Settings


# --------------------------------------------------------------------------- T3-1: config


def test_liveness_config_defaults() -> None:
    s = Settings()
    assert s.site_liveness_enabled is True
    assert s.site_liveness_auto_hide is False  # D1 — warn-first
    assert s.site_liveness_max_hides == 10  # D3
    assert s.nightly_dupes_write is True  # D2


def test_liveness_config_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SITE_LIVENESS_ENABLED", "false")
    monkeypatch.setenv("SITE_LIVENESS_AUTO_HIDE", "true")
    monkeypatch.setenv("SITE_LIVENESS_MAX_HIDES", "3")
    monkeypatch.setenv("NIGHTLY_DUPES_WRITE", "false")
    s = Settings()
    assert s.site_liveness_enabled is False
    assert s.site_liveness_auto_hide is True
    assert s.site_liveness_max_hides == 3
    assert s.nightly_dupes_write is False
