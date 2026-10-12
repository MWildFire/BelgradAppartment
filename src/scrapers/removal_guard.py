"""Removal guard (Q3.21): every removal step records its outcome, and auto-hides need them clean.

A step writes `running` when it starts, then `clean` or `alarm` with its counts, or `failed` when it
crashes. An alarm or a failure holds the guard for GUARD_HOURS from its time: a later clean or
running write does not replace it, a new alarm or failure does. The guard reads closed: a missing,
unreadable, unfinished, failed or alarmed outcome, or a clean one older than GUARD_HOURS, withholds
auto-hides. So the first night after deployment hides nothing until every enabled step has finished
once.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

GUARD_HOURS = 26
RUNNING = "running"
CLEAN = "clean"
ALARM = "alarm"
FAILED = "failed"
UNREADABLE = "unreadable"
_HOLDING = (ALARM, FAILED, UNREADABLE)

log = logging.getLogger(__name__)


def _path(state_dir: Path, step: str) -> Path:
    return state_dir / "removal_guard" / f"{step}.json"


def _load(path: Path) -> tuple[str, datetime] | None:
    """The status and time of a record. Missing gives None; unreadable gives UNREADABLE with the
    file's modification time, so it holds like an alarm inside the window."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(record["at"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        try:
            return UNREADABLE, datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return str(record["status"]), at


def _within(at: datetime, now: datetime) -> bool:
    """True when `at` lies in [now - GUARD_HOURS, now]; a future time is not inside the window."""
    return timedelta(0) <= now - at < timedelta(hours=GUARD_HOURS)


def _holds(at: datetime, now: datetime) -> bool:
    """True when an outcome written at `at` still holds. No lower bound: a future time holds too."""
    return now - at < timedelta(hours=GUARD_HOURS)


def write_step(
    state_dir: Path, step: str, *, status: str, now: datetime,
    deactivated: int = 0, attempted: int = 0,
) -> None:
    """Record one step's outcome atomically, unless an alarm or failure is still holding."""
    path = _path(state_dir, step)
    if status in (RUNNING, CLEAN):
        held = _load(path)
        if held and held[0] in _HOLDING and _holds(held[1], now):
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({
            "status": status, "at": now.isoformat(),
            "deactivated": deactivated, "attempted": attempted,
        }),
        encoding="utf-8",
    )
    temporary.replace(path)


def record_failure(state_dir: Path, step: str, *, now: datetime) -> None:
    """Best effort after a crash. If this write fails too, the running record still holds."""
    try:
        write_step(state_dir, step, status=FAILED, now=now)
    except OSError:
        log.exception("removal guard: could not record a %s failure", step)


def withholds_hides(state_dir: Path, *, now: datetime, steps: tuple[str, ...]) -> bool:
    """True unless every named step's latest outcome is clean and inside the window."""
    for step in steps:
        held = _load(_path(state_dir, step))
        if held is None or held[0] != CLEAN or not _within(held[1], now):
            return True
    return False
