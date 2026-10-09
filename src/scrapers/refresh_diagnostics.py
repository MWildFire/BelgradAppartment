"""Bounded refresh diagnostics. Never persist exception messages or listing identifiers."""

from __future__ import annotations

from src.db.models import Platform

ReasonCounts = dict[str, dict[str, dict[str, int]]]
STAGES = frozenset({"item", "skipped", "source", "persistence"})
INTERNAL_REASONS = frozenset({
    "incomplete_fields", "incomplete_photos", "incomplete_photo_layout",
    "detail_identity_mismatch", "empty_detail", "listing_deleted", "source_budget_exhausted",
})
EXCEPTION_CODES = {
    "TimeoutError": "timeout", "ReadTimeout": "timeout", "ConnectTimeout": "timeout",
    "PoolTimeout": "timeout", "WriteTimeout": "timeout", "CancelledError": "cancelled",
    "NoProxyAvailableError": "proxy_unavailable", "TargetClosedError": "browser_closed",
    "SourceListingRemovedError": "listing_removed", "HTTPStatusError": "http_error",
    "SourceListingInactiveError": "listing_inactive", "_MarkerMissing": "source_unconfirmed",
    "ConnectError": "network_error", "ReadError": "network_error",
    "RemoteProtocolError": "network_error", "PermissionError": "permission_denied",
    "OperationalError": "database_error", "IntegrityError": "database_error",
    "RuntimeError": "refresh_failed", "ValueError": "invalid_data",
    "TypeError": "invalid_data", "OSError": "io_error", "RefreshError": "refresh_failed",
}
REASONS = INTERNAL_REASONS | frozenset(EXCEPTION_CODES.values()) | {"http_forbidden"}


def reason_code(value: str | BaseException) -> str:
    if isinstance(value, BaseException):
        if type(value).__name__ == "HTTPStatusError":
            status = getattr(getattr(value, "response", None), "status_code", None)
            if status in (404, 410):
                return "listing_removed"
            if status == 403:
                return "http_forbidden"
        # Only these literal, internal RuntimeError messages are admitted; no interpolation.
        if type(value) is RuntimeError and len(value.args) == 1:
            message = value.args[0]
            if isinstance(message, str) and message in INTERNAL_REASONS:
                return message
        value = type(value).__name__
    return value if value in REASONS else EXCEPTION_CODES.get(value, "refresh_failed")


def source_error_code(value: str) -> str:
    """Retain known legacy class codes, without accepting arbitrary class names."""
    known = value in EXCEPTION_CODES or value == "source_budget_exhausted"
    return value if known else "RefreshError"


def add_reason(
    counts: ReasonCounts, platform: Platform, stage: str, code: str, delta: int = 1,
) -> None:
    if stage not in STAGES or code not in REASONS:
        raise ValueError("unsupported refresh diagnostic")
    reasons = counts.setdefault(platform.value, {}).setdefault(stage, {})
    reasons[code] = reasons.get(code, 0) + delta
    if reasons[code] <= 0:
        del reasons[code]
    if not reasons:
        del counts[platform.value][stage]
    if not counts[platform.value]:
        del counts[platform.value]


def read_reason_counts(value: object) -> ReasonCounts | None:
    """Reject malformed or unbounded metadata; missing legacy diagnostics use {}."""
    if not isinstance(value, dict) or not set(value) <= {p.value for p in Platform}:
        return None
    result: ReasonCounts = {}
    for platform, stages in value.items():
        if not isinstance(stages, dict) or not set(stages) <= STAGES:
            return None
        for stage, reasons in stages.items():
            if not isinstance(reasons, dict) or not set(reasons) <= REASONS:
                return None
            for code, count in reasons.items():
                if type(count) is not int or not 0 < count <= 2**63 - 1:
                    return None
                result.setdefault(platform, {}).setdefault(stage, {})[code] = count
    return result
