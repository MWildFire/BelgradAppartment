"""Durable, secret-free refresh progress and outcomes before optional notifications."""

from __future__ import annotations

import json
import os
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.config import get_settings

_CURRENT: ContextVar[dict[str, Any] | None] = ContextVar("refresh_journal", default=None)


def _save(payload: dict[str, Any]) -> Path:
    root = get_settings().data_dir / "state" / "refresh_runs"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"{payload['run_id']}.json"
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        os.chmod(temp, 0o600)
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    return path


def start_refresh(*, incremental: bool):
    now = datetime.now(UTC).isoformat()
    payload = {
        "version": 1,
        "run_id": uuid.uuid4().hex,
        "started_at": now,
        "heartbeat_at": now,
        "finished_at": None,
        "kind": "incremental" if incremental else "full",
        "status": "running",
        "step": "starting",
        "sources": [],
        "summary": [],
    }
    _save(payload)
    return _CURRENT.set(payload)


def progress(step: str, result: Any | None = None) -> None:
    payload = _CURRENT.get()
    if payload is None:
        return
    payload["step"] = step
    payload["heartbeat_at"] = datetime.now(UTC).isoformat()
    if result is not None:
        report = result.to_report()
        payload["sources"] = [s for s in payload["sources"] if s["platform"] != report["platform"]]
        payload["sources"].append(report)
    _save(payload)


def finish_refresh(token, *, error: str | None = None) -> None:
    payload = _CURRENT.get()
    try:
        if payload is not None:
            if payload["status"] == "running":
                payload["status"] = "failed" if error else "success"
            if error:
                payload["error_code"] = error
            payload["finished_at"] = datetime.now(UTC).isoformat()
            payload["heartbeat_at"] = payload["finished_at"]
            _save(payload)
    finally:
        _CURRENT.reset(token)


def record_refresh_result(
    results: list[Any],
    *,
    incremental: bool,
    status: str,
    summary: list[str],
) -> Path:
    now = datetime.now(UTC).isoformat()
    payload = _CURRENT.get() or {
        "version": 1,
        "run_id": uuid.uuid4().hex,
        "started_at": now,
        "kind": "incremental" if incremental else "full",
    }
    payload.update(
        finished_at=now,
        heartbeat_at=now,
        status=status,
        sources=[r.to_report() for r in results],
        summary=summary,
        step="finished",
    )
    return _save(payload)


def read_runs(root: Path | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
    folder = root or get_settings().data_dir / "state" / "refresh_runs"
    runs = []
    if not folder.exists():
        return runs
    for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[
        :limit
    ]:
        try:
            value = json.loads(path.read_text())
            if isinstance(value, dict) and value.get("version") == 1:
                runs.append(value)
        except (OSError, ValueError):
            continue
    return runs
