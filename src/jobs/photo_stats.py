"""Structured logger for photo-download outcomes (halooglasi CDN, etc.).

Every photo download attempt writes one JSON line to `data/logs/photo_downloads.jsonl`:
    {"ts": "2026-05-20T08:30:01+0000", "platform": "halooglasi", "listing_external_id": "...",
     "url": "...", "status": "ok"|"failed", "error": null|"<reason>"}

Rotated by the OS or via `logrotate`; for now one append-only file. Aggregation is one CLI
call away (`otz photo-stats`).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from src.config import get_settings

_LOGGER_NAME = "otz.photo_downloads"
_handler_attached = False


def _log_path() -> Path:
    p = get_settings().data_dir / "logs" / "photo_downloads.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _logger() -> logging.Logger:
    """Lazy-attach a file handler the first time the logger is touched.

    We avoid the root logger so this stream doesn't get mixed into systemd journal noise.
    """
    global _handler_attached
    lg = logging.getLogger(_LOGGER_NAME)
    if not _handler_attached:
        lg.setLevel(logging.INFO)
        lg.propagate = False  # keep these out of the main monitor.log
        handler = logging.handlers.RotatingFileHandler(
            _log_path(), maxBytes=20 * 1024 * 1024, backupCount=4, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(handler)
        _handler_attached = True
    return lg


def record_photo_download(
    platform: str,
    *,
    listing_external_id: str,
    url: str,
    status: Literal["ok", "failed"],
    error: str | None = None,
) -> None:
    """Append one JSON line per photo attempt. Cheap, write-only."""
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "platform": platform,
        "listing_external_id": listing_external_id,
        "url": url[:300],  # truncate noisy signed query strings
        "status": status,
    }
    if error:
        entry["error"] = error[:200]
    _logger().info(json.dumps(entry, ensure_ascii=False))


def aggregate(
    *,
    since_seconds: int = 24 * 3600,
    platform: str | None = None,
) -> dict[str, dict[str, int]]:
    """Tally ok/failed per platform from the JSONL log over the last `since_seconds` window."""
    cutoff = datetime.now(timezone.utc).timestamp() - since_seconds
    counts: dict[str, dict[str, int]] = {}
    log = _log_path()
    if not log.exists():
        return counts
    with log.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.strptime(entry["ts"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
            except (KeyError, ValueError):
                continue
            if ts < cutoff:
                continue
            plat = entry.get("platform", "?")
            if platform is not None and plat != platform:
                continue
            bucket = counts.setdefault(plat, {"ok": 0, "failed": 0, "total": 0})
            status = entry.get("status", "?")
            if status in bucket:
                bucket[status] += 1
            bucket["total"] += 1
    return counts
