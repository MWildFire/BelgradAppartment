"""Bound rsync's files-from exchange; never broaden the manifest allowlist."""
from pathlib import PurePosixPath


def batches(names, max_files=1000, max_bytes=128 * 1024):
    if max_files < 1 or max_bytes < 2:
        raise ValueError("invalid_batch_limit")
    seen = set()
    batch = []
    size = 0
    for name in names:
        if not isinstance(name, str):
            raise ValueError("invalid_or_duplicate_member")
        p = PurePosixPath(name)
        if (
            not name
            or "\0" in name
            or p.is_absolute()
            or ".." in p.parts
            or p.as_posix() != name
            or name in seen
        ):
            raise ValueError("invalid_or_duplicate_member")
        seen.add(name)
        length = len(name.encode()) + 1
        if length > max_bytes:
            raise ValueError("member_exceeds_batch_limit")
        if batch and (len(batch) >= max_files or size + length > max_bytes):
            yield batch
            batch = []
            size = 0
        batch.append(name)
        size += length
    if batch:
        yield batch


def encode_batch(names):
    return b"\0".join(name.encode() for name in names) + b"\0"
