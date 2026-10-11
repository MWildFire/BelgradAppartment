"""Pure fail-closed decisions for source quality and weekly source absence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class SourceEvaluation:
    outcome: str
    sweep_allowed: bool
    reasons: tuple[str, ...]


def evaluate_source(result) -> SourceEvaluation:
    """A bounded useful refresh may succeed; removal needs a proven complete full crawl."""
    reasons = []
    if result.errors:
        reasons.append("item_errors")
    if result.skipped_pages:
        reasons.append("skipped_pages")
    if result.pages_failed:
        reasons.append("failed_pages")
    if result.stop_reason in {"source_exception", "list_exception"}:
        reasons.append(result.stop_reason)
    if result.full_crawl:
        if not result.crawl_complete:
            reasons.append("crawl_incomplete")
        if not result.denominator_trusted or result.site_total is None or result.site_total < 0:
            reasons.append("untrusted_denominator")
        elif result.previewed != result.site_total:
            # Even apparent over-coverage makes the denominator questionable. No arbitrary
            # 50%/70% threshold can establish that missing listings really disappeared.
            reasons.append("coverage_mismatch")
        if len(result.observed_ids) != result.previewed:
            reasons.append("missing_presence_evidence")
        if result.fetched + result.errors != result.previewed:
            reasons.append("items_unprocessed")
        if result.pages_attempted <= 0:
            reasons.append("missing_page_evidence")
    if result.stop_reason == "item_limit":
        reasons.append("item_limit")
    if not result.previewed and not (
        result.full_crawl
        and result.crawl_complete
        and result.denominator_trusted
        and result.site_total == 0
        and result.pages_attempted > 0
    ):
        reasons.append("no_observations")
    reasons.extend(result.quality_reasons)
    # A few failing items are warnings: they still block the sweep, but the source stays ok.
    # When half or more of the processed items fail, the parser or the site is broken, so the
    # source degrades like any failed step.
    if result.errors and result.errors * 2 >= result.fetched + result.errors:
        reasons.append("item_error_rate")
    reasons = list(dict.fromkeys(reasons))
    # Item errors are warnings in the summary (see _nightly_unhealthy); they still block the
    # sweep through sweep_allowed, but on their own they do not degrade the source outcome.
    blocking = [reason for reason in reasons if reason != "item_errors"]
    outcome = "degraded" if blocking else "ok"
    if (
        result.stop_reason in {"source_exception", "list_exception"}
        or (result.errors and not result.fetched)
        or "no_observations" in reasons
    ):
        outcome = "failed"
    return SourceEvaluation(outcome, result.full_crawl and not reasons, tuple(reasons))


@dataclass(frozen=True)
class AbsenceDecision:
    streak: int
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    last_run_id: str | None
    deactivate: bool
    reason: str


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def evaluate_absence(
    *,
    observed: bool,
    streak: int,
    first_seen_at: datetime | None,
    last_seen_at: datetime | None,
    last_run_id: str | None,
    run_id: str,
    now: datetime,
    interval_days: int = 7,
    confirmations: int = 2,
) -> AbsenceDecision:
    """Count distinct, weekly-spaced successful full checks, never elapsed row age alone.

    A long gap resets confirmation: missed/failed weeks cannot be silently counted as a
    consecutive successful weekly absence. Replaying the same run is idempotent.
    """
    if observed:
        return AbsenceDecision(0, None, None, None, False, "observed")
    first, last, now = _utc(first_seen_at), _utc(last_seen_at), _utc(now)
    if last_run_id == run_id:
        return AbsenceDecision(streak, first, last, last_run_id, False, "already_evaluated")
    # Weekly jobs finish at different times. A one-day tolerance prevents a run finishing
    # eight minutes earlier than last week from starving confirmations forever.
    minimum = timedelta(days=max(6, interval_days - 1))
    if last is not None and now - last < minimum:
        return AbsenceDecision(streak, first, last, last_run_id, False, "confirmation_too_soon")
    if last is None or now - last > timedelta(days=max(7, interval_days) + 2):
        return AbsenceDecision(1, now, now, run_id, False, "first_weekly_absence")
    next_streak = streak + 1
    return AbsenceDecision(
        next_streak,
        first or now,
        now,
        run_id,
        next_streak >= max(2, confirmations),
        "confirmed_weekly_absence",
    )


# Share of one bounded probe sweep (link or liveness checks) that may fail before the sweep counts
# as broken rather than noisy: failed probes confirm nothing about the listings they did not reach.
# Owner decision: 0.5 is a proposal, not a measured baseline.
PROBE_FAILURE_ALARM_SHARE = 0.5

# Fewer attempts cannot tell a broken sweep from one transient failure, and a 🚨 fails the night.
# 10 is a Claude default, not a measured value.
ALARM_MIN_SAMPLE = 10


def probe_sweep_broken(*, checked: int, failed: int) -> bool:
    """True when probes failed wholesale (network down, blocked), not one by one.

    Alarm when every probe failed or the failed share is above PROBE_FAILURE_ALARM_SHARE, and the
    sweep ran at least ALARM_MIN_SAMPLE probes. Nothing checked with a failure, for example a source
    that errored before its first probe, counts as broken too.
    """
    if failed <= 0:
        return False
    if checked <= 0:
        return True
    if checked < ALARM_MIN_SAMPLE:
        return False
    if failed >= checked:
        return True
    return failed / checked > PROBE_FAILURE_ALARM_SHARE


# Share of the listings one run attempted that it deactivated as source-removed (404/410). Real
# churn is a few percent; a changed URL scheme or a broken route turns most attempts into 404s,
# and that must not pass as a normal run. Owner decision: 0.3 is a proposal, not a measured value.
REMOVAL_ALARM_SHARE = 0.3


def source_removal_alarm(*, deactivated: int, attempted: int) -> bool:
    """True when the removal share of one run is above REMOVAL_ALARM_SHARE, over at least
    ALARM_MIN_SAMPLE attempts.

    `attempted` counts every listing the run tried, including already removed ones.
    `deactivated` counts only new takedowns, never a re-check of a listing that was inactive.
    """
    return (
        deactivated > 0 and attempted >= ALARM_MIN_SAMPLE
        and deactivated / attempted > REMOVAL_ALARM_SHARE
    )
