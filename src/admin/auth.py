"""HTTP Basic-auth dependency for the admin UI.

Hardening (G2):
- Per-IP failed-attempt lockout to blunt brute-force. The store is in-process and per-IP,
  mirroring src/site/deps.py's lead_rate_limiter: single uvicorn worker per ADR, so a restart
  resets the counters — acceptable for this defence-in-depth control, not a hard boundary.
- The "ADMIN_PASSWORD is still the default" CRITICAL log fires once per process (the
  _default_password_warned flag short-circuits repeats) so it no longer spams every request.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from src.config import get_settings

logger = logging.getLogger(__name__)

_security = HTTPBasic()

_DEFAULT_PASSWORD = "change_me"
_default_password_warned = False

# --------------------------------------------------------------------------- Lockout config
# After LOCKOUT_MAX_FAILURES failed auths within LOCKOUT_WINDOW_SECONDS, an IP is locked out
# for LOCKOUT_COOLDOWN_SECONDS. A successful auth clears that IP's counter immediately.
LOCKOUT_MAX_FAILURES = 5
LOCKOUT_WINDOW_SECONDS = 15 * 60
LOCKOUT_COOLDOWN_SECONDS = 15 * 60

# Injectable monotonic clock — monkeypatched in tests so lockout-expiry is checkable without
# sleeping real time. Use monotonic (not wall-clock) so NTP/clock changes can't skew the window.
_now = time.monotonic


class _AdminLockout:
    """Per-IP failed-attempt tracker with a temporary lockout.

    In-process only (single worker per ADR); a restart resets state. Stale entries are evicted
    on access to guard against unbounded growth from churn of one-off IPs.
    """

    def __init__(self, clock=_now) -> None:  # type: ignore[no-untyped-def]
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (failure_count, first_failure_ts, locked_until_ts | None)
        self._state: dict[str, tuple[int, float, float | None]] = {}

    def retry_after(self, key: str) -> float | None:
        """Seconds until `key` may try again, or None if it is not currently locked out."""
        now = self._clock()
        with self._lock:
            self._evict(now)
            entry = self._state.get(key)
            if entry is None:
                return None
            _count, _first, locked_until = entry
            if locked_until is not None and locked_until > now:
                return locked_until - now
            return None

    def record_failure(self, key: str) -> None:
        """Count a failed attempt; trip the lockout once the budget is exhausted."""
        now = self._clock()
        with self._lock:
            self._evict(now)
            entry = self._state.get(key)
            if entry is None or now - entry[1] > LOCKOUT_WINDOW_SECONDS:
                count, first = 1, now
            else:
                count, first = entry[0] + 1, entry[1]
            locked_until = now + LOCKOUT_COOLDOWN_SECONDS if count >= LOCKOUT_MAX_FAILURES else None
            self._state[key] = (count, first, locked_until)

    def record_success(self, key: str) -> None:
        """A good login clears the IP's failure counter (and any lockout)."""
        with self._lock:
            self._state.pop(key, None)

    def _evict(self, now: float) -> None:
        """Drop entries that are neither locked out nor inside the failure window. Caller holds lock."""
        stale = [
            k
            for k, (_c, first, locked_until) in self._state.items()
            if (locked_until is None or locked_until <= now) and now - first > LOCKOUT_WINDOW_SECONDS
        ]
        for k in stale:
            del self._state[k]

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


# Process-wide store shared across requests.
_admin_lockout = _AdminLockout()


def _client_key(request: Request) -> str:
    """Client IP for the lockout store — same extraction as lead_rate_limiter (deps.py)."""
    client = request.client
    return client.host if client and client.host else "unknown"


def require_admin(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(_security),
) -> str:
    global _default_password_warned
    settings = get_settings()
    key = _client_key(request)

    # Locked-out IPs are turned away before the password is even checked.
    retry_after = _admin_lockout.retry_after(key)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed admin logins — try again later.",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    # Fail closed: a guessable default password must never grant access.
    if settings.admin_password == _DEFAULT_PASSWORD:
        if not _default_password_warned:
            logger.critical(
                "ADMIN_PASSWORD is still the default %r — rejecting ALL admin logins. "
                "Set a real ADMIN_PASSWORD in .env.",
                _DEFAULT_PASSWORD,
            )
            _default_password_warned = True
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin password not configured",
            headers={"WWW-Authenticate": 'Basic realm="otz-admin"'},
        )
    # compare_digest requires bytes (or ASCII-only str) — encode so non-ASCII
    # credentials get a clean 401 instead of a TypeError → HTTP 500.
    correct_user = secrets.compare_digest(
        credentials.username.encode("utf-8"), settings.admin_username.encode("utf-8")
    )
    correct_pass = secrets.compare_digest(
        credentials.password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )
    if not (correct_user and correct_pass):
        _admin_lockout.record_failure(key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="otz-admin"'},
        )
    _admin_lockout.record_success(key)
    return credentials.username
