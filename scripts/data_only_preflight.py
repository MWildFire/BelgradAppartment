#!/usr/bin/env python3
"""Local DB/media scope preflight, deliberately incompatible with off-host receipts.

No transport, schedules, protected files, environment loading or production activation.
Only run on synthetic data until a source-side inspection is explicitly authorized.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import recovery

FORMAT = "renfinder-db-media-preflight-v1"
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
MAX_CELL_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_SCAN_BYTES = 8 * 1024**3
CREDENTIAL_NAME = re.compile(
    r"(?i)(?:token|secret|password|passwd|credential|auth[_-]?key|api[_-]?key|cookie|session)"
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b"),
    # Exact token-body length avoids matching standard JPEG Huffman alphabet bytes.
    re.compile(rb"\b[0-9]{5,16}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
    re.compile(rb"(?i)\b(?:https?|socks5h?)://[^\s/:]+:[^\s/@]+@"),
    re.compile(rb"(?i)\b(?:bearer\s+|(?:api[_-]?key|password|passwd|auth[_-]?key|"
               rb"access[_-]?token|refresh[_-]?token|secret)\s*[\"']?\s*[:=]\s*[\"']?)\S{4,}"),
)


class PreflightError(Exception):
    """Only fixed codes are raised; never pass source paths or values as messages."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise PreflightError(code)


def secret_like(raw: bytes) -> bool:
    return any(pattern.search(raw) for pattern in SECRET_PATTERNS)


def identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def inspect_database(path: Path) -> dict:
    """Read a frozen copy; fail closed on populated credential columns or scan limits.

    This is a heuristic gate, not proof that arbitrary free text is credential-free.
    Diagnostics contain counts and fixed codes only, never table/column names or values.
    """
    cells = scanned = 0
    with recovery.ro_db(path) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        require(len(tables) <= 256, "schema_scan_limit")
        for table in tables:
            columns = [row[1] for row in connection.execute(
                f"PRAGMA table_info({identifier(table)})"
            )]
            require(len(columns) <= 256, "schema_scan_limit")
            for column in columns:
                quoted = identifier(column)
                source = identifier(table)
                if CREDENTIAL_NAME.search(column) or CREDENTIAL_NAME.search(table):
                    occupied = connection.execute(
                        f"SELECT 1 FROM {source} WHERE {quoted} IS NOT NULL "
                        f"AND length(CAST({quoted} AS BLOB)) > 0 LIMIT 1"
                    ).fetchone()
                    require(not occupied, "populated_credential_field")
                for length, raw in connection.execute(
                    f"SELECT length(CAST({quoted} AS BLOB)), "
                    f"substr(CAST({quoted} AS BLOB), 1, ?) FROM {source} "
                    f"WHERE typeof({quoted}) IN ('text', 'blob')", (MAX_CELL_BYTES + 1,),
                ):
                    require(length <= MAX_CELL_BYTES, "cell_scan_limit")
                    # SQLite substr() may return SQL NULL for a zero-byte BLOB, including
                    # CAST('' AS BLOB). Only a proven zero-length value can normalize to empty.
                    if length == 0 and raw is None:
                        raw = b""
                    require(isinstance(raw, bytes), "invalid_cell_type")
                    scanned += length
                    cells += 1
                    require(scanned <= MAX_SCAN_BYTES, "database_scan_limit")
                    require(not secret_like(raw), "credential_pattern_in_database")
    return {"result": "heuristic_scan_passed", "cells_scanned": cells, "bytes_scanned": scanned,
            "arbitrary_secrets_absence_proven": False}


def allowed_image(relative: Path) -> bool:
    """An allowlist, not the legacy broad data-tree exclusion list."""
    return (
        not relative.is_absolute() and ".." not in relative.parts
        and bool(relative.parts) and relative.suffix.lower() in IMAGE_SUFFIXES
        and all(not part.startswith(".") and not CREDENTIAL_NAME.search(part)
                and part.lower() not in {"state", "logs", "caddyfile", "id_rsa", "id_ed25519"}
                for part in relative.parts)
    )


def inspect_image(path: Path) -> dict:
    require(not path.is_symlink() and path.is_file(), "media_not_regular")
    before = path.stat()
    require(before.st_size <= MAX_IMAGE_BYTES, "image_scan_limit")
    with path.open("rb") as stream:
        raw = stream.read(MAX_IMAGE_BYTES + 1)
    require(len(raw) <= MAX_IMAGE_BYTES, "image_scan_limit")
    require(not secret_like(raw), "credential_pattern_in_media")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            require(image.format in {"JPEG", "PNG", "WEBP"}, "unsupported_image_format")
            require(image.width * image.height <= MAX_IMAGE_PIXELS, "image_scan_limit")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            width, height = image.size
            if image.getexif().get(274) in {5, 6, 7, 8}:
                width, height = height, width
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise PreflightError("invalid_image") from None
    after = path.stat()
    require((before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_ino, after.st_size, after.st_mtime_ns), "media_changed")
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "width": width, "height": height}


def prepare(args: argparse.Namespace) -> dict:
    """Freeze/compact a DB and inventory referenced images; never copy media or export."""
    project = args.project_root.absolute()
    require(project.resolve() == project, "symlink_project_root")
    source_db = project / "data/otz.db"
    require(source_db.resolve() == source_db and source_db.is_file(), "invalid_database_path")
    roots = {role: project / "data" / role for role in ("inbound", "listings")}
    for root in roots.values():
        require(root.resolve() == root and root.is_dir(), "invalid_media_root")
    output = args.output_dir.absolute()
    require(output.resolve() == output and not output.exists(), "invalid_output_path")
    require(not output.is_relative_to(project), "output_inside_source")
    recovery.private_dir(output.parent)
    with tempfile.TemporaryDirectory(prefix=".db-media-preflight-", dir=output.parent) as folder:
        staging = Path(folder)
        created_at = datetime.now(UTC).isoformat()
        (staging / "data").mkdir(mode=0o700)
        db = staging / "data/otz.db"
        with recovery.ro_db(source_db) as source, sqlite3.connect(db) as destination:
            os.chmod(db, 0o600)
            source.backup(destination, pages=1024, sleep=0.1)
            destination.execute("PRAGMA journal_mode=DELETE")
            # Compact only the generated copy; do not carry deleted/free-page payloads.
            destination.execute("VACUUM")
        inspection = inspect_database(db)
        database = recovery.database_manifest(db)
        selected: set[Path] = set()
        with recovery.ro_db(db) as connection:
            for (raw,) in connection.execute("SELECT photo_paths FROM inbound_posts"):
                paths = json.loads(raw or "[]")
                require(isinstance(paths, list), "invalid_photo_references")
                for stored in paths:
                    require(isinstance(stored, str), "invalid_photo_references")
                    path = Path(stored)
                    path = path if path.is_absolute() else project / path
                    require(path.is_relative_to(roots["inbound"]), "unsupported_photo_root")
                    selected.add(path)
            # Legacy listing albums have no complete stored path manifest. Their directory
            # is selected by DB identity; orphan directories are excluded.
            for platform, external_id in connection.execute(
                "SELECT platform, external_id FROM listings"
            ):
                require(platform in {"NEKRETNINE", "CETIRI_ZIDA", "HALOOGLASI", "CITYEXPERT"}
                        and isinstance(external_id, str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", external_id),
                        "unsafe_listing_identity")
                album = roots["listings"] / platform / external_id
                require(album.resolve() == album, "symlink_media_path")
                if album.exists():
                    require(album.is_dir(), "invalid_album")
                    selected.update(recovery.files_below(album))
        require(selected, "no_referenced_media")
        # Derived public variants may accompany referenced originals; the source index is
        # never copied (it can contain arbitrary metadata). Rebuild it in a future restore.
        for original in tuple(selected):
            for suffix in (".card.webp", ".detail.webp"):
                variant = Path(str(original) + suffix)
                if variant.exists():
                    selected.add(variant)
        media = {}
        for index, path in enumerate(sorted(selected), 1):
            require(path.resolve() == path, "symlink_media_path")
            relative = path.relative_to(project)
            require(allowed_image(relative), "media_outside_allowlist")
            media[relative.as_posix()] = inspect_image(path)
            progress = getattr(args, "progress", None)
            if progress is not None and (index % 1000 == 0 or index == len(selected)):
                progress("inspect_media", index, len(selected))
        manifest = {
            "format": FORMAT, "scope": "referenced_db_media_preflight",
            "created_at": created_at,
            "result": "local_preflight_only_not_restore_verified", "database": database,
            "database_file": {"size": db.stat().st_size, "sha256": recovery.digest(db)},
            "credential_inspection": inspection, "media": media,
            "excluded": ["state", "logs", "configuration", "sessions", "credentials",
                         "unreferenced_media", "source_photo_manifest"],
            "media_copied": False, "offhost_verified": False,
            "protected_recovery_verified": False, "health_receipt_published": False,
            "personal_data_present": True, "authorization_to_transfer": False,
        }
        inventory = staging / "manifest.json"
        inventory.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        inventory.chmod(0o600)
        staging.rename(output)
    return {"result": manifest["result"], "media_files": len(media),
            "credential_inspection": inspection, "authorization_to_transfer": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = prepare(args)
    except PreflightError as error:
        print(json.dumps({"result": "blocked", "reason": str(error)}))
        return 1
    except Exception:
        print(json.dumps({"result": "blocked", "reason": "preflight_failed"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
