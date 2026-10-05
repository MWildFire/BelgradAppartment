"""Provider-neutral validation outcomes; confidence is never publication authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["same", "different", "uncertain", "unavailable", "invalid_evidence"]


@dataclass(frozen=True)
class ValidationOutcome:
    verdict: Verdict
    reason_code: str
    confidence: float | None = None
    model: str = ""
    evidence_hash: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.verdict == "same"
