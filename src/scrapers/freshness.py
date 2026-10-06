"""Verified freshness evidence, fair source queues and bounded callback execution.

This module never fetches, changes listing content, publishes or sends messages itself.
The integration callback owns content/photo persistence and supplies explicit evidence;
metadata writes are a separate caller-controlled transaction via ``record_attempt``.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Listing, Match, Platform, SiteListing
from src.scrapers.proxy_pool import NoProxyAvailableError


@dataclass(frozen=True)
class RefreshCandidate:
    id: int
    platform: Platform
    url: str = field(repr=False)
    public: bool = False
    broken: bool = False
    active: bool = True
    succeeded_at: datetime | None = None
    attempted_at: datetime | None = None
    next_retry_at: datetime | None = None
    failure_count: int = 0


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _oldest(value: datetime | None) -> datetime:
    return utc(value) if value else datetime.min.replace(tzinfo=UTC)


def is_due(row: RefreshCandidate, now: datetime) -> bool:
    if row.next_retry_at and utc(row.next_retry_at) > utc(now):
        return False
    # Public runs at 08:00/14:00/19:00 UTC leave at most 13h05 between starts.
    # Eligibility at 10h leaves room for that gap and the 40-minute work budget.
    # Actual 24h freshness still requires enough capacity and successful source reads.
    interval = timedelta(hours=10) if row.public else timedelta(days=7)
    return (
        row.broken
        or row.succeeded_at is None
        or utc(now) - utc(row.succeeded_at) >= interval
        or utc(row.succeeded_at) > utc(now)
    )


def priority(row: RefreshCandidate) -> tuple:
    # A failed attempt does not make stale data look recent. Attempts break ties only.
    tier = 0 if row.public else 1 if row.broken else 2
    return tier, _oldest(row.succeeded_at), _oldest(row.attempted_at), row.id


async def load_candidates(
    session: AsyncSession,
    *,
    platforms: Iterable[Platform] | None = None,
    only_broken: bool = False,
    public_only: bool = False,
) -> list[RefreshCandidate]:
    """Read scalar metadata only, including retained public originals marked inactive.

    Return the full scoped inventory; ``select_fair`` filters retry eligibility. Keeping
    fresh/backoff rows in inventory makes quota/freshness reporting honest.
    """
    public = exists(
        select(SiteListing.id)
        .join(Match, Match.id == SiteListing.match_id)
        .where(
            Match.listing_id == Listing.id,
            SiteListing.is_public.is_(True),
        )
    )
    statement = select(
        Listing.id,
        Listing.platform,
        Listing.url,
        Listing.is_active,
        Listing.photo_phashes == [],
        Listing.details_succeeded_at,
        Listing.details_refreshed_at,
        Listing.details_next_retry_at,
        Listing.detail_failure_count,
        public,
    ).where(or_(Listing.is_active.is_(True), public))
    if platforms is not None:
        statement = statement.where(Listing.platform.in_(list(platforms)))
    if only_broken:
        statement = statement.where(Listing.photo_phashes == [])
    if public_only:
        statement = statement.where(public)
    records = (await session.execute(statement)).all()
    return [
        RefreshCandidate(
            id=row[0],
            platform=row[1],
            url=row[2],
            active=bool(row[3]),
            broken=bool(row[4]),
            succeeded_at=row[5],
            attempted_at=row[6],
            next_retry_at=row[7],
            failure_count=row[8] or 0,
            public=bool(row[9]),
        )
        for row in records
    ]


@dataclass(frozen=True)
class FairRefreshPlan:
    rows: tuple[RefreshCandidate, ...]
    source_order: tuple[Platform, ...]
    next_cursor: int
    eligible: int
    deferred: int
    inventory_count: int = 0
    retry_deferred: int = 0


def select_fair(
    candidates: Sequence[RefreshCandidate],
    *,
    limit: int,
    now: datetime | None = None,
    source_cursor: int = 0,
    due_only: bool = True,
    retry_backoff_cap: timedelta | None = None,
) -> FairRefreshPlan:
    """Round-robin quotas; unused source slots go to other eligible sources.

    Public/broken/oldest-success priority applies within each source, so one source cannot
    consume every slot. Persist ``next_cursor`` between runs when limit < source count.
    ``due_only=False`` allows preventive refreshes. An explicit retry cap can bound old
    exponential delays for a nightly inventory pass; recent attempts still cool down.
    """
    now = utc(now or datetime.now(UTC))
    if retry_backoff_cap is not None and retry_backoff_cap <= timedelta(0):
        raise ValueError("positive retry backoff cap required")
    def retry_due(row: RefreshCandidate) -> bool:
        if row.next_retry_at is None:
            return True
        retry_at = utc(row.next_retry_at)
        if retry_backoff_cap is not None and row.attempted_at is not None:
            retry_at = min(retry_at, utc(row.attempted_at) + retry_backoff_cap)
        return retry_at <= now
    require_unique = {row.id for row in candidates}
    if len(require_unique) != len(candidates):
        raise ValueError("duplicate listing IDs in freshness inventory")
    all_sources = sorted({row.platform for row in candidates}, key=lambda p: p.value)
    if not all_sources:
        return FairRefreshPlan((), (), 0, 0, 0)
    cursor = source_cursor % len(all_sources)
    rotation = all_sources[cursor:] + all_sources[:cursor]
    queues = {
        source: deque(
            sorted(
                (
                    row
                    for row in candidates
                    if row.platform == source
                    and retry_due(row)
                    and (not due_only or is_due(row, now))
                ),
                key=priority,
            )
        )
        for source in rotation
    }
    eligible = sum(len(queue) for queue in queues.values())
    selected = []
    last_source = None
    while len(selected) < max(0, limit) and any(queues.values()):
        for source in rotation:
            if queues[source] and len(selected) < max(0, limit):
                selected.append(queues[source].popleft())
                last_source = source
    next_cursor = (all_sources.index(last_source) + 1) % len(all_sources) if last_source else cursor
    source_order = tuple(dict.fromkeys(row.platform for row in selected))
    return FairRefreshPlan(
        tuple(selected), source_order, next_cursor, eligible, eligible - len(selected),
        len(candidates),
        sum(not retry_due(row) for row in candidates),
    )


@dataclass(frozen=True)
class RefreshEvidence:
    fields_complete: bool
    photos_complete: bool
    missing_fields: tuple[str, ...] = ()
    expected_photos: int = 0
    ready_photos: int = 0
    reason: str = ""

    @property
    def succeeded(self) -> bool:
        return self.fields_complete and self.photos_complete and self.expected_photos > 0

    @classmethod
    def failed(cls, reason: str) -> RefreshEvidence:
        # Reasons must be codes (e.g. TimeoutError), never exception messages/URLs/tokens.
        safe_reason = reason if reason.replace("_", "").isalnum() else "refresh_failed"
        return cls(False, False, reason=safe_reason)


def assess_detail(
    detail: Any,
    *,
    ready_photo_count: int,
    required_fields: Sequence[str] = ("price_eur", "area_m2", "photo_urls"),
) -> RefreshEvidence:
    """Validate values plus verified local photo coverage, not merely an HTTP 200.

    ``ready_photo_count`` must describe the actual usable files/hashes for this exact URL
    set, from successful downloads or verified existing files. A remote URL count is not
    local-photo evidence. Optional description/rooms are not required by default.
    """
    missing = []
    for name in required_fields:
        value = getattr(detail, name, None)
        if name in {"price_eur", "area_m2"}:
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value > 0
            )
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            valid = math.isfinite(value) and value >= 0
        elif isinstance(value, str):
            valid = bool(value.strip())
        else:
            valid = bool(value)
        if not valid:
            missing.append(name)
    expected = len(getattr(detail, "photo_urls", ()) or ())
    photos_complete = expected > 0 and ready_photo_count >= expected
    return RefreshEvidence(
        not missing,
        photos_complete,
        tuple(missing),
        expected,
        ready_photo_count,
        "incomplete_fields"
        if missing
        else "incomplete_photos"
        if not photos_complete
        else "complete",
    )


@dataclass(frozen=True)
class AttemptRecord:
    applied: bool
    succeeded: bool
    failure_count: int
    next_retry_at: datetime | None


async def record_attempt(
    session: AsyncSession,
    listing_id: int,
    evidence: RefreshEvidence,
    *,
    attempted_at: datetime,
    completed_at: datetime | None = None,
    base_backoff: timedelta = timedelta(minutes=30),
    max_backoff: timedelta = timedelta(hours=24),
    dry_run: bool = False,
) -> AttemptRecord:
    """Record attempted vs verified success separately; never backfill legacy successes.

    Call inside the same transaction as successful content/photo changes. On exceptions
    call in a fresh transaction with failed evidence. An older or repeated concurrent attempt cannot
    overwrite newer metadata. Caller commits; this helper never commits or publishes.
    """
    attempted_at = utc(attempted_at)
    completed_at = utc(completed_at or datetime.now(UTC))
    if completed_at < attempted_at:
        raise ValueError("completion predates attempt")
    if base_backoff.total_seconds() <= 0 or max_backoff < base_backoff:
        raise ValueError("invalid retry backoff")
    current = (
        await session.execute(
            select(
                Listing.details_refreshed_at,
                Listing.detail_failure_count,
            ).where(Listing.id == listing_id)
        )
    ).one_or_none()
    if current is None:
        return AttemptRecord(False, evidence.succeeded, 0, None)
    failed = 0 if evidence.succeeded else (current[1] or 0) + 1
    delay = min(
        max_backoff.total_seconds(), base_backoff.total_seconds() * 2 ** min(failed - 1, 16)
    )
    retry_at = None if evidence.succeeded else completed_at + timedelta(seconds=delay)
    if current[0] and utc(current[0]) >= attempted_at:
        return AttemptRecord(False, evidence.succeeded, current[1] or 0, None)
    if dry_run:
        return AttemptRecord(False, evidence.succeeded, failed, retry_at)
    values = dict(
        details_refreshed_at=attempted_at,
        detail_failure_count=failed,
        details_next_retry_at=retry_at,
    )
    if evidence.succeeded:
        # Existing future/newer success must not be moved backwards by a late callback.
        values["details_succeeded_at"] = func.max(
            func.coalesce(Listing.details_succeeded_at, completed_at),
            completed_at,
        )
    result = await session.execute(
        update(Listing)
        .where(
            Listing.id == listing_id,
            or_(
                Listing.details_refreshed_at.is_(None), Listing.details_refreshed_at < attempted_at
            ),
        )
        .values(**values)
        .execution_options(synchronize_session="fetch")
    )
    return AttemptRecord(bool(result.rowcount), evidence.succeeded, failed, retry_at)


@dataclass(frozen=True)
class RefreshExecution:
    row: RefreshCandidate
    attempted: bool
    evidence: RefreshEvidence
    attempted_at: datetime | None = None
    completed_at: datetime | None = None
    payload: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class CallbackResult:
    evidence: RefreshEvidence
    payload: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class FairExecutionResult:
    items: tuple[RefreshExecution, ...]
    source_errors: Mapping[str, str]
    elapsed_sec: float

    @property
    def attempted(self) -> int:
        return sum(item.attempted for item in self.items)

    @property
    def succeeded(self) -> int:
        return sum(item.attempted and item.evidence.succeeded for item in self.items)


@asynccontextmanager
async def _empty_context(_platform: Platform) -> AsyncIterator[None]:
    yield None


async def run_fair_refresh(
    plan: FairRefreshPlan,
    callback: Callable[[RefreshCandidate, Any], Awaitable[CallbackResult]],
    *,
    budget_sec: float,
    item_timeout_sec: float,
    context_factory: Callable[[Platform], AbstractAsyncContextManager[Any]] = _empty_context,
    source_weights: Mapping[Platform, float] | None = None,
    on_source_error: Callable[[Platform, str], None] | None = None,
) -> FairExecutionResult:
    """Reserve a share of remaining time for every remaining source; one context at a time.

    The source share covers startup, item work AND teardown. The item timeout also covers
    photo/embedding work in the callback. Contexts and callbacks must honor cancellation;
    process-kill isolation for a cancellation-resistant third party belongs to its adapter.
    Each source context is reused for its rows and released before acquiring the next lease.
    No DB/media writes occur unless the caller's callback explicitly performs them.
    """
    if budget_sec <= 0 or item_timeout_sec <= 0:
        raise ValueError("positive execution budgets required")
    started = time.monotonic()
    overall_deadline = started + budget_sec
    grouped: dict[Platform, list[RefreshCandidate]] = defaultdict(list)
    for row in plan.rows:
        grouped[row.platform].append(row)
    if (
        len(set(plan.source_order)) != len(plan.source_order)
        or set(plan.source_order) != set(grouped)
        or len({row.id for row in plan.rows}) != len(plan.rows)
    ):
        raise ValueError("invalid or duplicate rows/source order in refresh plan")
    weights = {source: 1.0 for source in plan.source_order}
    if source_weights is not None:
        if set(source_weights) != set(weights) or any(
            not math.isfinite(value) or value <= 0 for value in source_weights.values()
        ):
            raise ValueError("positive finite weights required for every selected source")
        weights = dict(source_weights)
    items = []
    source_errors = {}
    for index, platform in enumerate(plan.source_order):
        rows = grouped[platform]
        remaining_time = max(0.0, overall_deadline - time.monotonic())
        remaining_weight = sum(weights[source] for source in plan.source_order[index:])
        source_budget = remaining_time * weights[platform] / remaining_weight
        attempted_ids = set()
        in_flight = None
        attempted_at = None
        source_code = "source_budget_exhausted"
        if source_budget > 0:
            try:
                async with asyncio.timeout(source_budget), context_factory(platform) as context:
                    for row in rows:
                        in_flight, attempted_at = row, datetime.now(UTC)
                        attempted_ids.add(row.id)
                        try:
                            async with asyncio.timeout(item_timeout_sec):
                                result = await callback(row, context)
                            if not isinstance(result, CallbackResult) or not isinstance(
                                result.evidence, RefreshEvidence
                            ):
                                raise TypeError("callback must return CallbackResult with evidence")
                            evidence, payload = result.evidence, result.payload
                        except NoProxyAvailableError:
                            # No route remains for this source context. Continuing would
                            # record thousands of failed listings without another request
                            # and unnecessarily put every remaining row into backoff.
                            raise
                        except Exception as error:
                            evidence, payload = RefreshEvidence.failed(type(error).__name__), None
                        items.append(
                            RefreshExecution(
                                row, True, evidence, attempted_at, datetime.now(UTC), payload
                            )
                        )
                        in_flight = None
            except Exception as error:
                source_code = type(error).__name__
                source_errors[platform.value] = source_code
                if in_flight is not None:
                    items.append(
                        RefreshExecution(
                            in_flight,
                            True,
                            RefreshEvidence.failed(source_code),
                            attempted_at,
                            datetime.now(UTC),
                        )
                    )
        else:
            source_errors[platform.value] = source_code
        if platform.value in source_errors and on_source_error is not None:
            on_source_error(platform, source_code)
        for row in rows:
            if row.id not in attempted_ids:
                items.append(RefreshExecution(row, False, RefreshEvidence.failed(source_code)))
    return FairExecutionResult(tuple(items), source_errors, time.monotonic() - started)


def quota_estimates(
    candidates: Sequence[RefreshCandidate],
    *,
    now: datetime | None = None,
    measured_success_rates: Mapping[Platform, float] | None = None,
    all_inventory_daily: bool = False,
) -> dict[str, dict[str, int | float | None]]:
    """Report actual freshness and theoretical daily capacity needs, never attainment.

    Public originals require daily success; other active listings supply the remainder of
    the 95%-within-seven-days target. Rate-adjusted attempt quotas require a measured rate.
    All-inventory mode instead requires daily success for every scoped original.
    This is a capacity lower bound, not a promise that quotas overcome source failures.
    """
    now = utc(now or datetime.now(UTC))
    output = {}
    for platform in sorted({row.platform for row in candidates}, key=lambda p: p.value):
        rows = [row for row in candidates if row.platform == platform]
        active = [row for row in rows if row.active]
        public = [row for row in rows if row.public]
        public_active = sum(row.active for row in public)
        active_fresh = sum(
            row.succeeded_at is not None
            and timedelta(0) <= now - utc(row.succeeded_at) <= timedelta(days=7)
            for row in active
        )
        public_fresh = sum(
            row.succeeded_at is not None
            and timedelta(0) <= now - utc(row.succeeded_at) <= timedelta(hours=24)
            for row in public
        )
        active_fresh_daily = sum(
            row.succeeded_at is not None
            and timedelta(0) <= now - utc(row.succeeded_at) <= timedelta(hours=24)
            for row in active
        )
        target = len(active) if all_inventory_daily else math.ceil(len(active) * 0.95)
        daily_successes = len(public) + math.ceil(
            max(0, target - public_active) / (1 if all_inventory_daily else 7)
        )
        daily_plan = select_fair(
            rows, limit=len(rows), now=now, due_only=False,
            retry_backoff_cap=timedelta(hours=12),
        ) if all_inventory_daily else None
        rate = (measured_success_rates or {}).get(platform)
        if rate is not None and (not math.isfinite(rate) or not 0 <= rate <= 1):
            raise ValueError("measured success rate must be between zero and one")
        output[platform.value] = {
            "active": len(active),
            "fresh_active_7d": active_fresh,
            "fresh_active_24h": active_fresh_daily,
            "active_target_deficit": max(
                0, target - (active_fresh_daily if all_inventory_daily else active_fresh)
            ),
            "public": len(public),
            "fresh_public_24h": public_fresh,
            "public_target_deficit": len(public) - public_fresh,
            "broken": sum(row.broken for row in rows),
            "eligible_due": (
                daily_plan.eligible if daily_plan else sum(is_due(row, now) for row in rows)
            ),
            "retry_backoff": daily_plan.retry_deferred if daily_plan else sum(
                row.next_retry_at is not None and utc(row.next_retry_at) > now for row in rows
            ),
            "minimum_daily_successes": daily_successes,
            "measured_success_rate": rate,
            "estimated_daily_attempts": math.ceil(daily_successes / rate) if rate else None,
        }
    return output
