"""Shared pytest fixtures."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
from src.db.models import Base

FIXTURES = Path(__file__).parent / "fixtures"


@asynccontextmanager
async def fresh_db():
    """Fresh in-memory SQLite swapped in for the global session factory; yields (engine, factory).

    Context manager (not only a fixture) so tests can open several isolated DBs in one test.
    The WAL-file variant in test_monitor_dedup.py is deliberately separate (needs real
    multi-connection concurrency).
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    orig_engine = db_session._engine
    orig_factory = db_session._session_factory
    db_session._engine = engine
    db_session._session_factory = factory
    try:
        yield engine, factory
    finally:
        await engine.dispose()
        db_session._engine = orig_engine
        db_session._session_factory = orig_factory


@pytest_asyncio.fixture
async def db():
    """Per-test fresh_db; yields the engine."""
    async with fresh_db() as (engine, _factory):
        yield engine


def _read(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


@pytest.fixture
def nekretnine_list_html() -> str:
    return _read("nekretnine/list.html")


@pytest.fixture
def nekretnine_detail_html() -> str:
    return _read("nekretnine/detail.html")


@pytest.fixture
def cetiri_zida_list_html() -> str:
    return _read("cetiri_zida/list.html")


@pytest.fixture
def cetiri_zida_detail_html() -> str:
    return _read("cetiri_zida/detail.html")
