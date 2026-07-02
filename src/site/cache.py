"""Tiny in-process TTL cache for catalog/facets/map reads (DATA_CONTRACT §2, ADR perf).

The catalog changes a few times a day, so a 5-min process-local cache cuts the repeated
read-model joins to near zero without any cross-process coordination.

Bounded (Wave-5 #9): ключи каталога строятся из СЫРЫХ query-параметров, т.е. атакующий с
уникальными query-строками раньше растил _store без предела (expire-on-read чистил только
повторно запрошенный ключ — memory-DoS публичного GET /catalog). Теперь у кэша есть maxsize:
на set при переполнении сначала выкидываются истёкшие записи, затем старейшие по порядку
вставки (обычный dict сохраняет insertion order; set пере-вставляет ключ, так что «старейший» =
дольше всех не обновлявшийся). Живые горячие ключи переживают эвикцию истёкших.

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

# Потолок записей на кэш. Размер подобран с запасом: реальный keyspace — десятки комбинаций
# фильтров + страницы /apt; 512 записей каталога ≈ единицы MB (≈16.6 KB/страница из 24 view),
# т.е. худший случай при атаке уникальными query — ограниченный и безопасный.
CACHE_MAXSIZE = 512


class TTLCache:
    """Thread-safe TTL cache keyed by an opaque hashable key, bounded by `maxsize`.

    `ttl` defaults to settings.site_cache_ttl at call time. `clock` defaults to time.monotonic
    (monotonic so a wall-clock jump can't resurrect an expired entry); tests inject a fake.
    `maxsize` — жёсткий потолок записей: на set при переполнении эвиктится истёкшее, затем
    старейшее (см. module docstring).
    """

    def __init__(
        self,
        ttl: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        maxsize: int = CACHE_MAXSIZE,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._maxsize = max(int(maxsize), 1)  # 0/отрицательное не «безлимит», а минимум 1
        self._lock = threading.Lock()
        # key -> (expires_at_monotonic, value); insertion order == set-recency (см. set()).
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
                # Expire-on-read: сразу отдаём место (основную уборку делает эвикция в set()).
                del self._store[key]
                return MISS
            return value

    def set(self, key: Any, value: Any) -> None:
        ttl = self.ttl
        if ttl <= 0:
            return
        now = self._clock()
        with self._lock:
            # Пере-вставка: обновлённый ключ уходит в конец dict, так insertion order всегда
            # отражает «свежесть» set-а и старейший ключ — первый (дешёвый аналог OrderedDict).
            self._store.pop(key, None)
            if len(self._store) >= self._maxsize:
                # Сначала мусор: истёкшие записи никому не нужны — живые ключи их переживают.
                expired = [k for k, (exp, _) in self._store.items() if now >= exp]
                for k in expired:
                    del self._store[k]
            while len(self._store) >= self._maxsize:
                # Всё ещё полно (ничего не истекло) — жертвуем старейшими по порядку вставки.
                del self._store[next(iter(self._store))]
            self._store[key] = (now + ttl, value)

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


def invalidate_catalog_cache() -> None:
    """Clear all three read-model caches in THIS process.

    catalog_cache несёт не только страницы /catalog, но и ключи ("index",) / ("apt", id)
    (Wave-5 #13) — publish/hide сбрасывает и их этим же вызовом.
    Called whenever a SiteListing's visibility changes (auto-publish / auto-hide) so the
    flip shows up immediately for same-process callers. NOTE the cross-process limitation:
    the site runs as a separate long-lived process from the nightly orchestrator / Telegram
    delivery CLI, so this in-process clear does NOT reach a running site — there, staleness
    is bounded only by site_cache_ttl (default 300s = 5 min, i.e. ≤ a few minutes). That TTL
    is the cross-process freshness guarantee; this call is the same-process fast path.
    """
    catalog_cache.clear()
    facets_cache.clear()
    map_cache.clear()
