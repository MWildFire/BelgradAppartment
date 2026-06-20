"""Engines for the public catalog — strictly separate from src/db/session.py.

Two engines, by ADR isolation requirement:
- read-only engine: opened with OS-level `mode=ro` (SQLite URI) so the KERNEL — not a
  runtime flag — enforces read-only. Any write raises "attempt to write a readonly
  database" and CANNOT be re-enabled from SQL (`PRAGMA query_only=OFF` does not help) nor
  leak across pooled connections. query_only=ON is still set as belt-and-suspenders. Used
  for all catalog reads. (mode=ro reads a WAL DB fine while the pipeline writer holds it —
  verified empirically.) The DB file must already exist (the pipeline creates it).
- narrow writer engine: busy_timeout=5000, used ONLY for Lead inserts.

Tests pass a `database_url` override (a tmp FILE DB — ":memory:" gives each connection its
own DB, which would defeat both the read-only verification and writer/reader visibility).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import get_settings

# Cache (engine, factory) by URL so repeated calls (incl. per-test tmp DBs) reuse one engine.
_read_engines: dict[str, tuple[AsyncEngine, async_sessionmaker[AsyncSession]]] = {}
_writer_engines: dict[str, tuple[AsyncEngine, async_sessionmaker[AsyncSession]]] = {}


def _resolve_url(database_url: str | None) -> str:
    return database_url or get_settings().database_url


def _readonly_url(url: str) -> str:
    """Rewrite a sqlite URL to open the file OS-level read-only via the SQLite URI form.

    `sqlite+aiosqlite:////abs/otz.db` -> `sqlite+aiosqlite:///file:/abs/otz.db?mode=ro&uri=true`.
    SQLAlchemy consumes `uri=true` itself and passes `file:/abs/otz.db?mode=ro` to sqlite3.
    Left unchanged for non-sqlite, :memory:, or already-URI urls.
    """
    if not url.startswith("sqlite") or ":///" not in url:
        return url
    scheme, path = url.split(":///", 1)
    if not path or path.startswith("file:") or ":memory:" in path or "mode=ro" in path:
        return url
    return f"{scheme}:///file:{path}?mode=ro&uri=true"


def _read_factory(database_url: str | None) -> async_sessionmaker[AsyncSession]:
    url = _resolve_url(database_url)
    cached = _read_engines.get(url)
    if cached is None:
        # Engine is OS-level read-only (mode=ro); cache key stays the logical url so the
        # reader and the writer engine map to the same logical DB.
        engine = create_async_engine(_readonly_url(url), echo=False, future=True)
        if url.startswith("sqlite"):

            @event.listens_for(engine.sync_engine, "connect")
            def _read_pragmas(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA busy_timeout=5000")
                cur.execute("PRAGMA query_only=ON")  # any write -> readonly error
                cur.close()

        factory = async_sessionmaker(engine, expire_on_commit=False)
        _read_engines[url] = (engine, factory)
        return factory
    return cached[1]


def _writer_factory(database_url: str | None) -> async_sessionmaker[AsyncSession]:
    url = _resolve_url(database_url)
    cached = _writer_engines.get(url)
    if cached is None:
        engine = create_async_engine(url, echo=False, future=True)
        if url.startswith("sqlite"):

            @event.listens_for(engine.sync_engine, "connect")
            def _writer_pragmas(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA busy_timeout=5000")  # wait out the matcher's writes
                cur.close()

        factory = async_sessionmaker(engine, expire_on_commit=False)
        _writer_engines[url] = (engine, factory)
        return factory
    return cached[1]


@asynccontextmanager
async def read_session(database_url: str | None = None) -> AsyncIterator[AsyncSession]:
    """Read-only session for catalog reads. Writes raise OperationalError (readonly)."""
    factory = _read_factory(database_url)
    async with factory() as session:
        yield session


@asynccontextmanager
async def writer_session(database_url: str | None = None) -> AsyncIterator[AsyncSession]:
    """Narrow writer session — Lead inserts only. Commits on success, rolls back on error."""
    factory = _writer_factory(database_url)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engines() -> None:
    """Dispose all cached engines — call at shutdown / between isolated test DBs."""
    for engine, _factory in (*_read_engines.values(), *_writer_engines.values()):
        await engine.dispose()
    _read_engines.clear()
    _writer_engines.clear()
