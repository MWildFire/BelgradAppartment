"""Cooperative process shutdown and shared monotonic refresh deadlines."""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

T = TypeVar("T")

_DEADLINE: ContextVar[float | None] = ContextVar("refresh_deadline", default=None)


@contextmanager
def run_budget(seconds: float) -> Iterator[None]:
    token = _DEADLINE.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def remaining_budget(default: float, *, reserve: float = 0) -> float:
    deadline = _DEADLINE.get()
    if deadline is None:
        return default
    return min(default, max(0.0, deadline - time.monotonic() - reserve))


def run_cooperatively(coroutine: Coroutine[Any, Any, T]) -> T:
    """SIGTERM cancels through finally blocks so source checkpoints and journals survive."""

    async def main() -> T:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None
        previous = signal.getsignal(signal.SIGTERM)
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            return await coroutine
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, previous)

    try:
        return asyncio.run(main())
    except asyncio.CancelledError:
        raise SystemExit(143) from None
