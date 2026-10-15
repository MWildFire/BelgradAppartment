"""Clean photo copies: data/clean/<path relative to inbound_photos_dir> + a sidecar report.

Originals (InboundPost.photo_paths, data/inbound/...) are never modified. Next to each clean
copy lives `<name>.wm.json` with the algorithm version, the source sha256 and the report. A
repeated call with the same source and version reuses the result unless an unavailable
corner LaMa fallback can now be retried. When no marks are found,
no clean copy is created (the sidecar says changed=false) and consumers use the original.

Bump STORE_VERSION when templates or the algorithm change: everything is then recomputed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.config import PROJECT_ROOT, get_settings
from src.watermark.files import atomic_write
from src.watermark.generations import locked_token, read_token, replace_token

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


def _retry_available_corner(meta: dict[str, object]) -> bool:
    """Retry an unavailable LaMa fallback after recovery; preserve intentional keeps.

    This checks availability without importing OpenCV/torch or loading the model. It
    does not affect display reads, default skip results or explicit individual reverts.
    """
    settings = get_settings()
    if (
        meta.get("discarded")
        or settings.watermark_halo_corner_mode != "lama"
        or not settings.watermark_lama_enabled
    ):
        return False
    report = meta.get("report")
    marks = report.get("marks") if isinstance(report, dict) else None
    if not isinstance(marks, list) or not any(
        isinstance(mark, dict)
        and mark.get("template_id") == "halo_corner"
        and mark.get("method") == "skip"
        for mark in marks
    ):
        return False
    if not Path(settings.watermark_lama_model_path).is_file():
        return False
    try:
        return importlib.util.find_spec("torch") is not None
    except (ImportError, ValueError):
        return False


def _write_sidecar(clean: Path, payload: dict[str, object]) -> None:
    path = sidecar_for(clean)
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))


def _generation_path(clean: Path) -> Path:
    """Validate every derived path before creating the short-lived lock handle."""
    sidecar_for(clean)
    site_marker_for(clean)
    path = _checked_clean_path(clean.with_name(clean.name + ".wm.lock"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return _checked_clean_path(path)


def _cached_result(original: Path, clean: Path, meta: dict[str, object]) -> StoredResult:
    report = meta.get("report")
    return StoredResult(original, clean if meta.get("changed") else None,
                        report if isinstance(report, dict) else {}, reused=True)


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
    lock = _generation_path(clean)
    with locked_token(lock) as stream:
        source_bytes = original.read_bytes()
        digest = hashlib.sha256(source_bytes).hexdigest()
        meta = read_sidecar(clean)
        if (
            not force
            and meta is not None
            and not meta.get("publishing")
            and _matches_policy(meta)
            and not _retry_available_corner(meta)
            and meta.get("sha256") == digest
            and (not meta.get("changed") or clean.is_file())
        ):
            return _cached_result(original, clean, meta)
        token = uuid.uuid4().hex.encode("ascii")
        replace_token(stream, token)
        version = STORE_VERSION
        mode = get_settings().watermark_halo_corner_mode
        quality = get_settings().watermark_jpeg_quality

    # lazy: keeps OpenCV out of the site and admin imports
    from src.watermark.pipeline import clean_file

    # Neither an obsolete worker nor a failed encoder may touch the public clean path.
    with tempfile.TemporaryDirectory(prefix=f".{clean.name}.wm-", dir=clean.parent) as work:
        snapshot = Path(work) / "original.jpg"
        staged = Path(work) / "clean.jpg"
        snapshot.write_bytes(source_bytes)
        report = clean_file(snapshot, staged, quality=quality)
        with locked_token(_generation_path(clean)) as stream:
            if (
                read_token(stream) != token
                or _sha256(original) != digest
                or version != STORE_VERSION
                or mode != get_settings().watermark_halo_corner_mode
            ):
                raise RuntimeError("watermark result superseded by a newer request or source")
            if report.changed and (not staged.is_file() or not staged.stat().st_size):
                raise RuntimeError("watermark result is incomplete")
            # A crash/failure between the two atomic replacements leaves a retryable,
            # unselected pending record, never an accepted image with another report.
            _write_sidecar(clean, {
                "version": version, "halo_corner_mode": mode, "sha256": digest,
                "changed": False, "publishing": True, "report": {},
            })
            if report.changed:
                os.replace(staged, _checked_clean_path(clean))
            elif clean.exists():
                clean.unlink()  # stale derivative, never the original
            _write_sidecar(clean, {
                "version": version, "halo_corner_mode": mode, "sha256": digest,
                "changed": report.changed,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "source": str(original), "report": report.to_dict(),
            })
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
    with locked_token(_generation_path(clean)) as stream:
        replace_token(stream, uuid.uuid4().hex.encode("ascii"))
        # Persist opt-out before removing derivatives, so even a failed unlink is safe.
        _write_sidecar(clean, {
            "version": STORE_VERSION,
            "halo_corner_mode": get_settings().watermark_halo_corner_mode,
            "sha256": digest,
            "changed": False,
            "discarded": True,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": str(original),
            "report": {"discarded": True},
        })
        if clean.exists():
            clean.unlink()
        marker.unlink(missing_ok=True)
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
        if (
            meta is None or meta.get("publishing")
            or not _matches_policy(meta) or _retry_available_corner(meta)
        ):
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
