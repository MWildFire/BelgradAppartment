"""Unit tests for src.clientip.client_ip — real client IP behind Caddy/Cloudflare.

Trust boundary (DATA_CONTRACT-S §IP):
  1. settings.trust_cloudflare AND CF-Connecting-IP present  -> that header (real client).
  2. else X-Forwarded-For present -> RIGHTMOST entry (the peer Caddy actually saw + appended;
     leftmost entries are client-supplied and spoofable).
  3. else request.client.host (or "unknown").

Spoofing guard: when trust_cloudflare is OFF, CF-Connecting-IP is IGNORED (it is freely
forgeable until the origin is CF-IP-locked + the flag is on).

The Request is built from a minimal ASGI scope, so these are pure unit tests (no app/server).
Integration coverage (lead rate-limit + admin G2 lockout key on the resolved IP) lives in
tests/site/test_integration.py and tests/test_admin_auth.py respectively.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from src.clientip import client_ip
from src.config import get_settings


def _request(
    *,
    client: tuple[str, int] | None = ("127.0.0.1", 12345),
    headers: dict[str, str] | None = None,
) -> Request:
    """Minimal ASGI request: a peer `client` and case-insensitive header pairs."""
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
        "client": client,
    }
    return Request(scope)


@pytest.fixture
def trust_cf(monkeypatch):
    """Flip settings.trust_cloudflare True for the duration of a test."""
    monkeypatch.setattr(get_settings(), "trust_cloudflare", True)


@pytest.fixture(autouse=True)
def _untrust_cf_by_default(monkeypatch):
    """Default-off: pin trust_cloudflare=False so test order / .env can't leak it on."""
    monkeypatch.setattr(get_settings(), "trust_cloudflare", False)


# --------------------------------------------------------------------------- CF header


def test_cf_header_used_when_trust_enabled(trust_cf):
    req = _request(headers={"CF-Connecting-IP": "1.2.3.4"})
    assert client_ip(req) == "1.2.3.4"


def test_cf_header_wins_over_xff_when_trusted(trust_cf):
    req = _request(headers={"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "9.9.9.9"})
    assert client_ip(req) == "1.2.3.4"


def test_cf_header_case_insensitive(trust_cf):
    req = _request(headers={"cf-connecting-ip": "1.2.3.4"})
    assert client_ip(req) == "1.2.3.4"


def test_cf_header_is_stripped(trust_cf):
    req = _request(headers={"CF-Connecting-IP": "  1.2.3.4  "})
    assert client_ip(req) == "1.2.3.4"


def test_cf_header_ignored_when_trust_disabled_falls_to_peer():
    # Spoof attempt: flag off -> CF-Connecting-IP is forgeable, must NOT be trusted.
    req = _request(client=("203.0.113.7", 5), headers={"CF-Connecting-IP": "9.9.9.9"})
    assert client_ip(req) == "203.0.113.7"


def test_cf_header_ignored_when_trust_disabled_falls_to_xff():
    req = _request(headers={"CF-Connecting-IP": "9.9.9.9", "X-Forwarded-For": "evil, 5.6.7.8"})
    assert client_ip(req) == "5.6.7.8"


def test_empty_cf_header_falls_through_even_when_trusted(trust_cf):
    req = _request(client=("203.0.113.7", 5), headers={"CF-Connecting-IP": "   "})
    assert client_ip(req) == "203.0.113.7"


# --------------------------------------------------------------------------- XFF


def test_xff_rightmost_entry_is_returned():
    # Leftmost ("evil") is client-supplied/spoofable; rightmost is the peer Caddy appended.
    req = _request(headers={"X-Forwarded-For": "evil, 5.6.7.8"})
    assert client_ip(req) == "5.6.7.8"


def test_xff_single_entry():
    req = _request(headers={"X-Forwarded-For": "5.6.7.8"})
    assert client_ip(req) == "5.6.7.8"


def test_xff_rightmost_stripped():
    req = _request(headers={"X-Forwarded-For": "evil ,  5.6.7.8 "})
    assert client_ip(req) == "5.6.7.8"


def test_xff_trailing_empty_entry_falls_back_to_prior():
    # "a, b, " -> rightmost non-empty is "b".
    req = _request(headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2, "})
    assert client_ip(req) == "2.2.2.2"


def test_empty_xff_falls_to_peer():
    req = _request(client=("203.0.113.7", 5), headers={"X-Forwarded-For": "   "})
    assert client_ip(req) == "203.0.113.7"


# --------------------------------------------------------------------------- peer fallback


def test_no_proxy_headers_returns_peer():
    req = _request(client=("203.0.113.7", 5))
    assert client_ip(req) == "203.0.113.7"


def test_no_client_returns_unknown():
    req = _request(client=None)
    assert client_ip(req) == "unknown"
