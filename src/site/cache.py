"""Tiny in-process TTL cache for catalog/facets/map reads (DATA_CONTRACT §2, ADR perf).

The catalog changes a few times a day, so a 5-min process-local cache cuts the repeated
read-model joins to near zero without any cross-process coordination. Deliberately small:
no eviction policy beyond TTL expiry-on-read — the keyspace is bounded by the handful of
filter combinations real visitors use.

POST /lead and /healthz never go through here (the routes simply don't call it).

Unit-testable: the clock is injectable (`clock=`), and `ttl=0` disables caching entirely
so tests can assert "no stale value is ever returned".
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from src.config import get_settings

# Sentinel distinct from None, so a cached None is still a cache HIT. Public so callers can
# branch on a miss without a magic value (`if cache.get(k) is not cache.MISS`).
MISS = object()
_MISS = MISS  # back-compat alias used internally/in tests


class TTLCache:
    """Thread-safe TTL cache keyed by an opaque hashable key.

    `ttl` defaults to settings.site_cache_ttl at call time. `clock` defaults to time.monotonic
    (monotonic so a wall-clock jump can't resurrect an expired entry); tests inject a fake.
    """

    def __init__(
        self, ttl: int | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (expires_at_monotonic, value)
        self._store: dict[Any, tuple[float, Any]] = {}

    @property
    def ttl(self) -> int:
        # Resolve lazily so a settings change (or test override) is honoured per-call.
        return get_settings().site_cache_ttl if self._ttl is None else self._ttl

    def get(self, key: Any) -> Any:
        """Return the cached value or the MISS sentinel (so a cached None is a hit)."""
        if self.ttl <= 0:
            return MISS
        now = self._clock()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return MISS
            expires_at, value = entry
            if now >= expires_at:
                # Expire-on-read: drop the stale entry so the dict can't grow unbounded.
                del self._store[key]
                return MISS
            return value

    def set(self, key: Any, value: Any) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._store[key] = (self._clock() + self.ttl, value)

    def get_or_set(self, key: Any, producer: Callable[[], Any]) -> Any:
        """Return the cached value, else call `producer()`, store, and return it.

        `producer` is called at most once per miss (outside the lock — the producer is an
        async-driven readmodel call wrapped by the caller; we only guard the dict here).
        """
        cached = self.get(key)
        if cached is not MISS:
            return cached
        value = producer()
        self.set(key, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


def normalize_catalog_key(params: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    """Stable cache key from query params: drop empties, sort, normalize lists to tuples.

    Two requests with the same effective filters (regardless of param order or empty values)
    must collapse to one key, else the cache never hits.
    """
    items: list[tuple[str, Any]] = []
    for k in sorted(params):
        v = params[k]
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, (list, tuple)):
            v = tuple(sorted(str(x) for x in v))
        items.append((k, v))
    return tuple(items)


# Process-wide caches, one per read kind so eviction/keys don't collide.
catalog_cache = TTLCache()
facets_cache = TTLCache()
map_cache = TTLCache()
