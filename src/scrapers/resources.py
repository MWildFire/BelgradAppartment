"""Cross-process leases for source traffic and persistent browser profiles."""

from __future__ import annotations

import asyncio
import fcntl
import os
import time
from pathlib import Path


class ResourceBusyError(TimeoutError):
    """Required resource stayed busy until the caller's bounded queue deadline."""


class ResourceLease:
    def __init__(self, path: Path, *, wait_seconds: float = 60.0):
        self.path = path
        self.wait_seconds = wait_seconds
        self._file = None
        self.waited_seconds = 0.0

    async def acquire(self):
        if self._file is not None:
            return self
        started = time.monotonic()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        os.chmod(self.path, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._file = handle
                    self.waited_seconds = time.monotonic() - started
                    return self
                except BlockingIOError:
                    if time.monotonic() - started >= self.wait_seconds:
                        raise ResourceBusyError("source resource queue deadline exceeded") from None
                    await asyncio.sleep(min(0.1, max(0.001, self.wait_seconds)))
        except BaseException:
            handle.close()
            raise

    def release(self):
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    async def __aenter__(self):
        return await self.acquire()

    async def __aexit__(self, *_exc):
        self.release()
