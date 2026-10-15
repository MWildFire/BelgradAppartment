"""Clean photo copies: data/clean/<path relative to inbound_photos_dir> + a sidecar report.

Originals (InboundPost.photo_paths, data/inbound/...) are never modified. Next to each clean
copy lives `<name>.wm.json` with the algorithm version, the source sha256 and the report. A
repeated call with the same source and version recomputes nothing. When no marks are found,
no clean copy is created (the sidecar says changed=false) and consumers use the original.

Bump STORE_VERSION when templates or the algorithm change: everything is then recomputed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.config import PROJECT_ROOT, get_settings
from src.watermark.files import atomic_write

logger = logging.getLogger(__name__)

STORE_VERSION = 2
SIDECAR_SUFFIX = ".wm.json"
SITE_MARKER_SUFFIX = ".site"


@dataclass(frozen=True)
class StoredResult:
    original: Path
    clean: Path | None  # None: no marks, use the original
    report: dict[str, object]
    reused: bool

    @property
    def best(self) -> Path:
        return self.clean if self.clean is not None else self.original


def resolve_stored(stored: str) -> Path | None:
    """InboundPost.photo_paths entry -> existing absolute file (same rules as src/site/photos)."""
    if not stored or "://" in stored:
        return None
    path = Path(stored)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        path = path.resolve()
    except OSError:
        return None
    return path if path.is_file() else None


def clean_root() -> Path:
    """Clean copies must not be written inside inbound (originals stay untouched)."""
    root = Path(get_settings().watermark_clean_dir).resolve()
    inbound = Path(get_settings().inbound_photos_dir).resolve()
    if root.is_relative_to(inbound):  # equal to inbound or inside it
        raise RuntimeError(f"WATERMARK_CLEAN_DIR {root} must lie outside {inbound}")
    return root


def _checked_clean_path(path: Path) -> Path:
    """Reject derived paths redirected through symlinks, including sidecars/markers."""
    root = clean_root()
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"unsafe clean path: {path}") from error
    if not resolved.is_relative_to(root) or resolved != path.absolute():
        raise RuntimeError(f"unsafe clean path outside its expected location: {path}")
    return path


def clean_path_for(original: Path) -> Path | None:
    """data/inbound/<rel> -> data/clean/<rel> (.jpg); None when the file is outside inbound."""
    inbound = Path(get_settings().inbound_photos_dir).resolve()
    try:
        rel = original.resolve().relative_to(inbound)
    except (OSError, ValueError):
        return None
    target = clean_root() / rel
    if target.suffix.lower() not in (".jpg", ".jpeg"):
        target = target.with_name(target.name + ".jpg")
    return _checked_clean_path(target)


def sidecar_for(clean: Path) -> Path:
    return _checked_clean_path(clean.with_name(clean.name + SIDECAR_SUFFIX))


def site_marker_for(clean: Path) -> Path:
    """Marker next to a clean copy: its mtime is when the site variants were built from it."""
    return _checked_clean_path(clean.with_name(clean.name + SITE_MARKER_SUFFIX))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_sidecar(clean: Path) -> dict[str, object] | None:
    try:
        data = json.loads(sidecar_for(clean).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _matches_policy(meta: dict[str, object]) -> bool:
    """Mode switches invalidate copies; an explicit discard remains in effect."""
    return meta.get("version") == STORE_VERSION and (
        bool(meta.get("discarded"))
        or meta.get("halo_corner_mode") == get_settings().watermark_halo_corner_mode
    )


def _write_sidecar(clean: Path, payload: dict[str, object]) -> None:
    path = sidecar_for(clean)
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))


def ensure_clean(stored: str, *, force: bool = False) -> StoredResult | None:
    """Make sure one photo has an up-to-date clean copy. None: the file is not on disk.

    A photo outside data/inbound (for example an already clean copy from a frozen delivery
    payload) is returned as is, and no clean copy is created for it.
    """
    original = resolve_stored(stored)
    if original is None:
        return None
    clean = clean_path_for(original)
    if clean is None:
        return StoredResult(
            original=original, clean=None, report={"outside_inbound": True}, reused=True
        )
    digest = _sha256(original)
    meta = read_sidecar(clean)
    if (
        not force
        and meta is not None
        and _matches_policy(meta)
        and meta.get("sha256") == digest
        and (not meta.get("changed") or clean.is_file())
    ):
        report = meta.get("report")
        return StoredResult(
            original=original,
            clean=clean if meta.get("changed") else None,
            report=report if isinstance(report, dict) else {},
            reused=True,
        )

    # lazy: keeps OpenCV out of the site and admin imports
    from src.watermark.pipeline import clean_file

    report = clean_file(original, clean, quality=get_settings().watermark_jpeg_quality)
    if not report.changed and clean.exists():
        clean.unlink()  # stale copy from an older algorithm version
    _write_sidecar(
        clean,
        {
            "version": STORE_VERSION,
            "halo_corner_mode": get_settings().watermark_halo_corner_mode,
            "sha256": digest,
            "changed": report.changed,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": str(original),
            "report": report.to_dict(),
        },
    )
    return StoredResult(
        original=original,
        clean=clean if report.changed else None,
        report=report.to_dict(),
        reused=False,
    )


def discard(original: Path) -> bool:
    """Drop the clean copy of one photo and remember the decision: the sidecar then says
    changed=false (with discarded=true), so sweeps and /clean keep using the original until
    `ensure_clean(..., force=True)`. Returns False for files outside the inbound tree."""
    clean = clean_path_for(original)
    if clean is None:
        return False
    # Validate all derived paths and read the source before any deletion.
    sidecar_for(clean)
    marker = site_marker_for(clean)
    digest = _sha256(original)
    if clean.exists():
        clean.unlink()
    marker.unlink(missing_ok=True)
    _write_sidecar(
        clean,
        {
            "version": STORE_VERSION,
            "halo_corner_mode": get_settings().watermark_halo_corner_mode,
            "sha256": digest,
            "changed": False,
            "discarded": True,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": str(original),
            "report": {"discarded": True},
        },
    )
    return True


def clean_or_original(original: Path) -> Path:
    """For display: the clean copy when it exists for the current version, else the original."""
    try:
        clean = clean_path_for(original)
        if clean is None or not clean.is_file():
            return original
        meta = read_sidecar(clean)
        if meta is None or not _matches_policy(meta) or not meta.get("changed"):
            return original
        return clean
    except RuntimeError:  # unsafe clean paths: show the original rather than fail the page
        return original


def is_current(original: Path) -> bool:
    """True when a current sidecar is not older than the original; no source hashing."""
    try:
        clean = clean_path_for(original)
        if clean is None:
            return True
        if sidecar_for(clean).stat().st_mtime_ns < original.stat().st_mtime_ns:
            return False
        meta = read_sidecar(clean)
        if meta is None or not _matches_policy(meta):
            return False
        return not meta.get("changed") or clean.is_file()
    except RuntimeError as error:
        # Do not repeatedly schedule a job that cannot safely write to this location.
        logger.warning("[watermark] unsafe clean storage for %s: %s", original, error)
        return True
    except OSError:
        return False


def site_source(original: Path) -> Path:
    """What the site shows instead of the original: the clean copy if WATERMARK_SITE_USE_CLEAN=1."""
    if not get_settings().watermark_site_use_clean:
        return original
    return clean_or_original(original)
