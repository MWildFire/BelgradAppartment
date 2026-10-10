"""Bound rsync's files-from exchange; never broaden the manifest allowlist."""
from pathlib import PurePosixPath

# One rsync call moves at most MAX_CONTENT_BYTES of file content and at most 1000 names. A batch
# that runs longer than the 7200 s transport timeout fails. A failed batch is not retried: the run
# stops, and a resume deletes the partial copy and transfers again. A full batch of 4 GiB takes
# 7200 s at about 600 kB/s, so a channel slower than that fails every full batch. The staged
# database of 2026-09-10 was 1,496,510,464 bytes (1.39 GiB), about 2.9x below this cap.
MAX_CONTENT_BYTES = 4 * 1024**3


def batches(names, max_files=1000, max_bytes=128 * 1024, sizes=None, max_content_bytes=None):
    """Split names into batches of at most max_files names and max_bytes encoded name bytes.

    When sizes maps every name to its file size, a batch also holds at most max_content_bytes of
    file content. Without an explicit value the limit is MAX_CONTENT_BYTES as it is when this call
    runs, not when the module was imported. A file above that limit cannot be batched:
    ValueError("file_over_batch_cap"). The receiver refuses such a snapshot before its first
    transfer, on the manifest size.
    """
    if max_content_bytes is None:
        max_content_bytes = MAX_CONTENT_BYTES
    if max_files < 1 or max_bytes < 2 or max_content_bytes < 1:
        raise ValueError("invalid_batch_limit")
    seen = set()
    batch = []
    size = 0
    content = 0
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
        file_bytes = 0
        if sizes is not None:
            file_bytes = sizes.get(name)
            if type(file_bytes) is not int or file_bytes < 0:
                raise ValueError("invalid_file_size")
            if file_bytes > max_content_bytes:
                raise ValueError("file_over_batch_cap")
        if batch and (
            len(batch) >= max_files
            or size + length > max_bytes
            or content + file_bytes > max_content_bytes
        ):
            yield batch
            batch = []
            size = 0
            content = 0
        batch.append(name)
        size += length
        content += file_bytes
    if batch:
        yield batch


def encode_batch(names):
    return b"\0".join(name.encode() for name in names) + b"\0"
