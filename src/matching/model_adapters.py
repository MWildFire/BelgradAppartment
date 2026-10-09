"""Offline provider contracts for a measured migration. No SDK, credentials or network here.

The production caller still uses Anthropic. Candidate requests must be evaluated and
explicitly approved before wiring a new network transport into the running bot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from src.matching.llm_validator import SYSTEM_PROMPT, DuplicateCheck, LLMValidatorError
from src.matching.validation_contracts import ValidationOutcome, Verdict


@dataclass(frozen=True)
class ModelProfile:
    provider: Literal["anthropic", "openai"]
    model: str
    input_usd_per_million: float
    output_usd_per_million: float
    effort: str | None = None


# Official prices checked 2026-10-05. Estimates are not observed bills or quality scores.
PROFILES = {
    "baseline": ModelProfile("anthropic", "claude-haiku-4-5", 1, 5),
    "luna": ModelProfile("openai", "gpt-6-luna", 0.10, 0.50, "none"),
    "sonnet": ModelProfile("anthropic", "claude-sonnet-5-5", 2, 10),
}


def estimate_cost(profile: ModelProfile, input_tokens: int, output_tokens: int) -> float:
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("Token counts cannot be negative")
    return (
        input_tokens * profile.input_usd_per_million
        + output_tokens * profile.output_usd_per_million
    ) / 1_000_000


def build_request(
    profile: ModelProfile, normalized_content: list[dict[str, Any]]
) -> dict[str, Any]:
    """Translate the same normalized evidence into native REST request contracts."""
    schema = DuplicateCheck.model_json_schema()
    schema["required"] = list(schema["properties"])
    for property_schema in schema["properties"].values():
        property_schema.pop("default", None)
    if profile.provider == "anthropic":
        return {
            "model": profile.model,
            "max_tokens": 400,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": normalized_content}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
    content = []
    for block in normalized_content:
        if block["type"] == "text":
            content.append({"type": "input_text", "text": block["text"]})
        elif block["type"] == "image":
            source = block["source"]
            content.append(
                {
                    "type": "input_image",
                    "detail": "high",
                    "image_url": f"data:{source['media_type']};base64,{source['data']}",
                }
            )
        else:
            raise ValueError("Unsupported evidence block")
    return {
        "model": profile.model,
        "store": False,
        "max_output_tokens": 400,
        "instructions": SYSTEM_PROMPT,
        "input": [{"role": "user", "content": content}],
        "reasoning": {"effort": profile.effort},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "apartment_identity",
                "strict": True,
                "schema": schema,
            }
        },
    }


def parse_response(
    profile: ModelProfile, body: dict[str, Any], *, threshold: float = 0.7
) -> ValidationOutcome:
    """Fixture/native REST response → common result; refusal/truncation never means different."""
    try:
        if profile.provider == "anthropic":
            if body.get("stop_reason") != "end_turn":
                raise LLMValidatorError("incomplete_response")
            blocks = body["content"]
            text = "".join(b["text"] for b in blocks if b["type"] == "text")
        else:
            if body.get("status") != "completed":
                raise LLMValidatorError("incomplete_response")
            blocks = [
                b
                for item in body["output"]
                if item.get("type") == "message"
                for b in item.get("content", [])
            ]
            if any(b["type"] == "refusal" for b in blocks):
                raise LLMValidatorError("refusal")
            text = "".join(b["text"] for b in blocks if b["type"] == "output_text")
        check = DuplicateCheck.model_validate_json(text)
        usage = {
            k: v
            for k, v in body.get("usage", {}).items()
            if k in {"input_tokens", "output_tokens"} and isinstance(v, int)
        }
        verdict: Verdict = (
            "uncertain"
            if check.uncertain or check.confidence < threshold
            else ("same" if check.is_same_apartment else "different")
        )
        return ValidationOutcome(
            verdict,
            "model_verdict",
            check.confidence,
            body.get("model", profile.model),
            usage=usage,
        )
    except Exception:
        return ValidationOutcome("unavailable", "response_invalid", model=profile.model)


def fixture_response(profile: ModelProfile, check: dict[str, Any]) -> dict[str, Any]:
    """Synthetic contract fixture, never a claim about model accuracy."""
    body: dict[str, Any] = {
        "model": profile.model,
        "usage": {"input_tokens": 6000, "output_tokens": 200},
    }
    if profile.provider == "anthropic":
        body.update(stop_reason="end_turn", content=[{"type": "text", "text": json.dumps(check)}])
    else:
        body.update(
            status="completed",
            output=[
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(check)}]}
            ],
        )
    return body
