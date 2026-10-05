"""Run synthetic provider contract checks; no visual quality measurement, keys or network."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.matching.model_adapters import (
    PROFILES,
    build_request,
    estimate_cost,
    fixture_response,
    parse_response,
)

CASES: list[tuple[str, dict[str, Any], str]] = [
    (
        "same",
        {
            "is_same_apartment": True,
            "confidence": 0.99,
            "uncertain": False,
            "reasoning": "synthetic",
        },
        "same",
    ),
    (
        "different",
        {
            "is_same_apartment": False,
            "confidence": 0.99,
            "uncertain": False,
            "reasoning": "synthetic",
        },
        "different",
    ),
    (
        "low-positive",
        {
            "is_same_apartment": True,
            "confidence": 0.4,
            "uncertain": False,
            "reasoning": "synthetic",
        },
        "uncertain",
    ),
    (
        "low-negative",
        {
            "is_same_apartment": False,
            "confidence": 0.4,
            "uncertain": False,
            "reasoning": "synthetic",
        },
        "uncertain",
    ),
    (
        "uncertain",
        {
            "is_same_apartment": True,
            "confidence": 0.99,
            "uncertain": True,
            "reasoning": "synthetic",
        },
        "uncertain",
    ),
    ("malformed", {"is_same_apartment": "not-a-boolean"}, "unavailable"),
]


def run() -> dict[str, Any]:
    results = []
    for name, profile in PROFILES.items():
        request = build_request(profile, [{"type": "text", "text": "synthetic contract only"}])
        assert request["model"] == profile.model
        for case_id, response, expected in CASES:
            outcome = parse_response(profile, fixture_response(profile, response))
            results.append(
                {
                    "profile": name,
                    "case": case_id,
                    "expected": expected,
                    "actual": outcome.verdict,
                    "passed": outcome.verdict == expected,
                }
            )
    return {
        "scope": "offline contracts only; no model calls or visual quality measurement",
        "actual_api_calls": 0,
        "actual_cost_usd": 0,
        "estimated_single_call_6000in_200out_usd": {
            name: estimate_cost(profile, 6000, 200) for name, profile in PROFILES.items()
        },
        "cases": results,
        "passed": all(row["passed"] for row in results),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    raise SystemExit(0 if report["passed"] else 1)
