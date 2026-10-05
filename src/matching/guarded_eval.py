"""Offline-only candidate adapter with the exact reserve/dispatch/settle lifecycle.

There is deliberately no live transport argument, credential lookup or network SDK.
Passing these tests authorizes neither paid calls nor transfer of real photographs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from src.matching.eval_budget import BudgetLedger, Quote
from src.matching.model_adapters import ModelProfile, build_request, parse_response
from src.matching.validation_contracts import ValidationOutcome


@dataclass(frozen=True)
class FixtureResult:
    outcome: ValidationOutcome
    request_sha256: str
    simulated_requests: int
    actual_api_calls: int = 0


def evaluate_fixture(
    ledger: BudgetLedger,
    *,
    attempt_id: str,
    stage: str,
    profile: ModelProfile,
    content: list[dict[str, Any]],
    response: dict[str, Any],
    max_input_tokens: int,
    fault: Literal["timeout", "cancel_before_send"] | None = None,
) -> FixtureResult:
    """Explicit fixture only. Amounts represent simulated charges, not an API bill."""
    request = build_request(profile, content)
    request_hash = hashlib.sha256(
        json.dumps(request, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    quote = Quote(
        profile.model,
        max_input_tokens,
        400,
        str(profile.input_usd_per_million),
        str(profile.output_usd_per_million),
    )
    reservation = ledger.reserve(attempt_id, stage, request_hash, quote)
    if fault == "cancel_before_send":
        ledger.cancel_unsent(reservation)
        return FixtureResult(ValidationOutcome("unavailable", "fixture_canceled"), request_hash, 0)
    ledger.dispatch(reservation)
    if fault == "timeout":
        ledger.unknown(reservation)
        return FixtureResult(ValidationOutcome("unavailable", "fixture_timeout"), request_hash, 1)
    usage = response.get("usage", {})
    extra = set(usage) - {"input_tokens", "output_tokens"} if isinstance(usage, dict) else set()
    # Cache creation/read prices and provider-specific dimensions require their own
    # conservative quotes. Never refund based on a partial pair of token counters.
    if extra:
        ledger.unpriced(reservation)
        return FixtureResult(ValidationOutcome("unavailable", "unpriced_usage"), request_hash, 1)
    # Schema/refusal errors can still be billable: account usage before parsing a verdict.
    if (
        isinstance(usage, dict)
        and type(usage.get("input_tokens")) is int
        and type(usage.get("output_tokens")) is int
        and usage["input_tokens"] >= 0
        and usage["output_tokens"] >= 0
    ):
        ledger.settle(
            reservation,
            model=str(response.get("model", "")),
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
    else:
        ledger.unknown(reservation)
    return FixtureResult(parse_response(profile, response), request_hash, 1)
