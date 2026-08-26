from __future__ import annotations

import pytest

from src.config import Settings
from src.scrapers.proxy_pool import (
    NoProxyAvailableError,
    ProxyCandidate,
    ProxyConfigurationError,
    ProxyPool,
)


def test_snapshot_mode_is_typed_and_defaults_to_legacy() -> None:
    assert Settings(_env_file=None).nekretnine_list_snapshot_mode == "legacy"
    with pytest.raises(ValueError):
        Settings(_env_file=None, nekretnine_list_snapshot_mode="unknown")


def test_proxy_secrets_are_not_in_settings_or_candidate_repr() -> None:
    settings = Settings(
        _env_file=None,
        SCRAPER_PROXY_URL="http://user:super-secret@proxy.example:8080",
    )
    assert "super-secret" not in repr(settings)
    candidate = ProxyCandidate(
        slot="primary", kind="static_isp", url="http://user:super-secret@proxy.example:8080"
    )
    assert "super-secret" not in repr(candidate)
    assert candidate.describe() == {
        "slot": "primary",
        "kind": "static_isp",
        "host": "http://proxy.example:8080",
    }


def test_static_isp_passes_credentials_without_session_suffix() -> None:
    candidate = ProxyCandidate(
        slot="primary", kind="static_isp", url="http://user:password@proxy.example:8080"
    )
    assert candidate.materialize(session_token="abc123") == candidate.url


def test_legacy_rotating_replaces_stale_suffix() -> None:
    candidate = ProxyCandidate(
        slot="primary",
        kind="legacy_rotating",
        url="http://user:password_session-old_lifetime-30m@proxy.example:8080",
    )
    value = candidate.materialize(session_token="abc123")
    assert "password_session-abc123_lifetime-30m" in value
    assert "old" not in value


def test_pool_orders_primary_secondary_metered_and_fails_closed() -> None:
    settings = Settings(
        _env_file=None,
        nekretnine_proxy_mode="static_isp",
        nekretnine_proxy_primary_url="http://p.example:1",
        nekretnine_proxy_secondary_url="http://s.example:2",
        nekretnine_proxy_metered_url="http://m.example:3",
    )
    pool = ProxyPool.from_settings(settings)
    assert [c.slot for c in pool.candidates] == ["primary", "secondary", "metered"]
    assert pool.select().slot == "primary"
    assert pool.select(failed_slots={"primary"}).slot == "secondary"
    assert pool.select(failed_slots={"primary", "secondary"}).slot == "metered"
    with pytest.raises(NoProxyAvailableError):
        pool.select(failed_slots={"primary", "secondary", "metered"})


def test_pool_includes_additional_static_candidates_before_metered() -> None:
    settings = Settings(
        _env_file=None,
        nekretnine_proxy_mode="static_isp",
        nekretnine_proxy_primary_url="http://p.example:1",
        nekretnine_proxy_secondary_url="http://s.example:2",
        nekretnine_proxy_additional_urls="http://a.example:3,\nhttp://b.example:4",
        nekretnine_proxy_metered_url="http://m.example:5",
    )
    pool = ProxyPool.from_settings(settings)
    assert [candidate.slot for candidate in pool.candidates] == [
        "primary",
        "secondary",
        "additional-1",
        "additional-2",
        "metered",
    ]
    assert [candidate.kind for candidate in pool.candidates[2:4]] == ["static_isp", "static_isp"]
    assert pool.select(failed_slots={"primary", "secondary"}).slot == "additional-1"
    assert pool.select(failed_slots={"primary", "secondary", "additional-1"}).slot == "additional-2"


def test_pool_deduplicates_additional_urls_without_leaking_values() -> None:
    settings = Settings(
        _env_file=None,
        nekretnine_proxy_mode="static_isp",
        nekretnine_proxy_primary_url="http://p.example:1",
        nekretnine_proxy_additional_urls="http://p.example:1,http://a.example:2,http://a.example:2",
    )
    pool = ProxyPool.from_settings(settings)
    assert [candidate.slot for candidate in pool.candidates] == ["primary", "additional-2"]
    assert "http://a.example:2" not in repr(pool.candidates[1])


def test_malformed_config_is_fail_closed() -> None:
    settings = Settings(_env_file=None, nekretnine_proxy_primary_url="not-a-proxy")
    with pytest.raises(ProxyConfigurationError):
        ProxyPool.from_settings(settings)


def test_legacy_scraper_proxy_is_primary_compatibility_fallback() -> None:
    settings = Settings(_env_file=None, scraper_proxy_url="http://legacy.example:8080")
    pool = ProxyPool.from_settings(settings)
    assert pool.select().url == "http://legacy.example:8080"


def test_legacy_scraper_proxy_remains_bounded_fallback_when_primary_is_new() -> None:
    settings = Settings(
        _env_file=None,
        scraper_proxy_url="http://legacy.example:8080",
        nekretnine_proxy_mode="static_isp",
        nekretnine_proxy_primary_url="http://isp.example:1",
    )
    pool = ProxyPool.from_settings(settings)
    assert [candidate.slot for candidate in pool.candidates] == ["primary", "metered"]
    assert pool.candidates[1].kind == "legacy_rotating"
    assert pool.select(failed_slots={"primary"}).url == "http://legacy.example:8080"
