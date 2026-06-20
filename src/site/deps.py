"""FastAPI dependencies for the public catalog: DB-url seam, site CSRF, lead rate-limit.

- `catalog_db_url` is the single TEST SEAM: routes read the DB through it, integration tests
  override it via app.dependency_overrides to point at a seeded temp DB.
- Site CSRF mirrors src/admin/csrf.py but with a SEPARATE per-process token, so the admin
  token never validates a public POST and vice-versa.
- The lead rate-limiter is an in-memory per-IP sliding window — defence against form spam,
  not a hard security boundary (ADR: "rate-limit by IP, memory/table").
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from src.config import get_settings


def catalog_db_url() -> str:
    """DB url for all catalog reads. The test seam — overridden in tests."""
    return get_settings().database_url


# --------------------------------------------------------------------------- Site CSRF
# Separate token from the admin one (admin is localhost+basic-auth; the public site is
# internet-facing, so its token must be independent). Same synchronizer-token pattern.

SITE_CSRF_TOKEN = secrets.token_urlsafe(32)

CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"

_FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


async def require_site_csrf(request: Request) -> None:
    """Fail-closed: any POST without a valid site token (header or form field) → 403."""
    token = request.headers.get(CSRF_HEADER)
    if not token and request.headers.get("content-type", "").startswith(_FORM_CONTENT_TYPES):
        # Starlette caches request.form(), so a later Form()/await request.form() still works.
        value = (await request.form()).get(CSRF_FORM_FIELD)
        token = value if isinstance(value, str) else None
    if not token or not secrets.compare_digest(token, SITE_CSRF_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing or invalid",
        )


# --------------------------------------------------------------------------- Rate limiter
# Per-IP sliding window. In-process only (single uvicorn worker per ADR); a restart resets
# counters, which is acceptable for spam mitigation.


class _RateLimiter:
    """Sliding-window per-key limiter. `max` hits allowed per `window` seconds."""

    def __init__(self, clock=time.monotonic) -> None:  # type: ignore[no-untyped-def]
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, max_hits: int, window: int) -> bool:
        """Record a hit for `key`; return True if it is within the limit, False if over."""
        if max_hits <= 0:
            return True  # limiting disabled
        now = self._clock()
        cutoff = now - window
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= max_hits:
                return False
            dq.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# Process-wide limiter shared across requests.
lead_rate_limiter = _RateLimiter()


async def enforce_lead_ratelimit(request: Request) -> None:
    """Reject (429) a client IP that exceeds SITE_LEAD_RATELIMIT_MAX in the window."""
    settings = get_settings()
    client = request.client
    key = client.host if client and client.host else "unknown"
    ok = lead_rate_limiter.check(
        key,
        settings.site_lead_ratelimit_max,
        settings.site_lead_ratelimit_window,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests — please try again later.",
        )
