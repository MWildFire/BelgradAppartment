"""Async SQLAlchemy engine + session factory."""

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

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str | None = None) -> AsyncEngine:
    """Create (or return) the process-wide async engine.

    For SQLite we enable WAL journaling so multiple processes (e.g. the long-running
    scrape and a separately-launched scrape-only-for-platform job) can read/write
    concurrently without "database is locked" errors.
    """
    global _engine, _session_factory
    if _engine is None:
        url = database_url or get_settings().database_url
        _engine = create_async_engine(url, echo=False, future=True)
        if url.startswith("sqlite"):
            @event.listens_for(_engine.sync_engine, "connect")
            def _enable_wal(dbapi_conn, _connection_record):  # type: ignore[no-untyped-def]
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=10000")  # wait up to 10s on lock
                cur.execute("PRAGMA synchronous=NORMAL")
                # SQLite ships with FK enforcement OFF — without this, ondelete=CASCADE
                # declared on our models is silently ignored.
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Async context manager yielding a session that commits on success, rolls back on error."""
    factory = _factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close all pooled connections — call at shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
