"""Comparable marketplace inventory counts from one completed full crawl."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.db.models import Platform

MARKETPLACES = (Platform.NEKRETNINE, Platform.CETIRI_ZIDA, Platform.HALOOGLASI)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def market_coverage(
    runs: list[dict[str, Any]], *, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Use processed (persisted) rows, never all historical ``is_active`` rows.

    A source's processed count and site total must belong to the same full crawl. A
    partial crawl still yields a measured lower bound; an untrusted or dated site
    total never yields a percentage. Incremental runs cannot measure market share.
    """
    now = now or datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for platform in MARKETPLACES:
        chosen: tuple[datetime, dict[str, Any]] | None = None
        for run in runs:
            if run.get("kind") != "full" or not run.get("finished_at"):
                continue
            finished = _timestamp(run["finished_at"])
            if finished is None or finished > now:
                continue
            for source in run.get("sources", []):
                if source.get("platform") != platform.value:
                    continue
                if chosen is None or finished > chosen[0]:
                    chosen = finished, source
        row: dict[str, Any] = {"platform": platform.value, "in_database": None, "site_total": None,
               "missing": None, "percent": None, "as_of": None, "status": "unknown"}
        if chosen is not None:
            finished, source = chosen
            count, total = source.get("processed"), source.get("site_total")
            row["as_of"] = finished
            if type(count) is int and count >= 0:
                row["in_database"] = count
            if type(total) is int and total >= 0:
                row["site_total"] = total
            if (
                source.get("denominator_trusted") is True
                and type(count) is int and count >= 0
                and type(total) is int and total >= count and total > 0
                and now - finished <= timedelta(hours=30)
            ):
                row["missing"] = total - count
                row["percent"] = round(100 * count / total, 1)
                row["status"] = "complete" if source.get("crawl_complete") is True else "partial"
            elif now - finished > timedelta(hours=30):
                row["status"] = "dated"
            else:
                row["status"] = "unverified"
        rows.append(row)
    return rows
