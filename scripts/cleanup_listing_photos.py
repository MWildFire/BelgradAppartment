"""Remove photo directories for archived listings that no longer exist in the database.

The archive and database must be on the same host. Run without --apply first.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
from pathlib import Path

PLATFORMS = {"NEKRETNINE", "CETIRI_ZIDA", "HALOOGLASI", "CITYEXPERT"}


def candidates(archive: Path, database: Path, root: Path) -> tuple[list[Path], int]:
    """Fully validate the gzip and all paths before the first removal."""
    if not root.is_dir() or root.is_symlink():
        raise ValueError("photo root is missing or a symlink")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        remaining = set(connection.execute("SELECT platform, external_id FROM listings"))
    finally:
        connection.close()
    safe: list[Path] = []
    rows = 0
    seen: set[tuple[str, str]] = set()
    with gzip.open(archive, "rt", encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            rows += 1
            platform, external_id = record.get("platform"), record.get("external_id")
            if (platform not in PLATFORMS or not isinstance(external_id, str)
                    or not external_id or external_id in {".", ".."}
                    or "/" in external_id or "\\" in external_id):
                raise ValueError(f"unsafe archive row {rows}")
            key = platform, external_id
            if key in seen:
                continue  # same-day archive may contain multiple gzip members
            seen.add(key)
            if key in remaining:
                continue  # a match protected this row or the listing was reinserted
            platform_dir = root / platform
            if not platform_dir.is_dir() or platform_dir.is_symlink():
                raise ValueError(f"unsafe platform directory: {platform}")
            path = platform_dir / external_id
            if path.is_symlink():
                raise ValueError(f"symlink photo directory at archive row {rows}")
            if path.is_dir():
                safe.append(path)
    return safe, rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    paths, rows = candidates(args.archive, args.database, args.root)
    print(
        f"archive_rows={rows} unreferenced_photo_dirs={len(paths)} apply={args.apply}",
        flush=True,
    )
    if args.apply:
        for path in paths:
            shutil.rmtree(path)
        print(f"removed_photo_dirs={len(paths)}", flush=True)


if __name__ == "__main__":
    main()
