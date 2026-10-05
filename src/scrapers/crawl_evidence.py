"""Shared crawl counters: totals come only from source payloads, never observed counts."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


def source_total_report(scraper: CrawlEvidence) -> dict[str, Any]:
    """Snapshot only generated count telemetry, independently of later scraper reuse."""
    return dict(deepcopy(getattr(scraper, "_source_total_telemetry", {})))


class CrawlEvidence:
    def reset_crawl_evidence(self):
        self._pages_attempted = 0
        self._pages_failed = 0
        self._crawl_complete = False
        self._crawl_stop_reason = "page_limit"
        self._site_total_trusted = False
        self._crawl_issues = []
        self._stopped_known = False
        self._source_total_telemetry = {
            "observations": 0, "first": None, "last": None, "min": None, "max": None,
            "change_count": 0, "first_observation": None, "last_observation": None,
            "changes": [], "changes_omitted": 0,
        }

    def record_page_attempt(self, *, observed=0):
        self._pages_attempted += 1
        self._observed_count = observed
        callback = getattr(self, "_progress_callback", None)
        if callback is not None:
            callback()

    def record_crawl_failure(self, reason):
        self._pages_failed += 1
        self._crawl_issues.append(reason)
        self._crawl_stop_reason = reason

    def record_source_total(self, value, *, scope="source", page=None):
        if type(value) is not int or value < 0:
            return
        telemetry = self._source_total_telemetry
        observation = {
            "scope": scope if scope in {"source", "base"} else "source",
            "page": page if type(page) is int and page > 0 else None,
            "observed_at": datetime.now(UTC).isoformat(), "total": value,
        }
        previous = telemetry["last"]
        if previous is None:
            telemetry.update(first=value, min=value, max=value, first_observation=observation)
        elif previous != value:
            telemetry["change_count"] += 1
            if len(telemetry["changes"]) < 64:
                telemetry["changes"].append({"previous": previous, **observation})
            else:
                telemetry["changes_omitted"] += 1
        telemetry.update(
            observations=telemetry["observations"] + 1, last=value,
            min=min(telemetry["min"], value), max=max(telemetry["max"], value),
            last_observation=observation,
        )
        if self._site_total is not None and self._site_total != value:
            self._crawl_issues.append("source_total_changed")
        if self._site_total is None:
            self._site_total = value

    def finish_crawl_evidence(self, observed):
        self._site_total_trusted = (
            type(self._site_total) is int
            and self._site_total >= 0
            and "source_total_changed" not in self._crawl_issues
        )
        self._crawl_complete = bool(
            self._site_total_trusted
            and observed == self._site_total
            and self._pages_attempted > 0
            and not self._pages_failed
            and not self._skipped_pages
            and not self._crawl_issues
            and not self._stopped_known
        )
        if self._crawl_complete:
            self._crawl_stop_reason = "exhausted"
        elif self._crawl_issues:
            self._crawl_stop_reason = self._crawl_issues[0]
        elif not self._site_total_trusted:
            self._crawl_stop_reason = "untrusted_denominator"
        elif self._stopped_known:
            self._crawl_stop_reason = "known_items"
        else:
            self._crawl_stop_reason = "coverage_incomplete"
