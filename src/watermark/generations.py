"""Short cross-process locks and generation tokens for one derived photo.

Inference runs outside the lock. New work and explicit discard invalidate older
generations; only the current generation may publish its staged result.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


@contextmanager
def locked_token(path: Path) -> Iterator[BinaryIO]:
    # Lazy import keeps disabled-feature imports usable without this Unix facility.
    import fcntl

    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeError("unsafe watermark generation lock")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield stream
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_token(stream: BinaryIO) -> bytes:
    stream.seek(0)
    return stream.read(128)


def replace_token(stream: BinaryIO, token: bytes) -> None:
    stream.seek(0)
    stream.truncate()
    stream.write(token)
    stream.flush()
    os.fsync(stream.fileno())
