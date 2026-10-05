from __future__ import annotations

import pytest

from src.matching.model_adapters import PROFILES, build_request, estimate_cost, parse_response
from src.matching.offline_eval import run


def test_offline_provider_contract_matrix():
    report = run()
    assert report["passed"] and len(report["cases"]) == 18
    assert report["actual_api_calls"] == report["actual_cost_usd"] == 0


def test_openai_request_uses_structured_output_and_no_storage():
    content = [{"type": "image", "source": {"media_type": "image/jpeg", "data": "fixture"}}]
    request = build_request(PROFILES["luna"], content)
    assert request["store"] is False
    assert request["input"][0]["content"][0]["image_url"] == "data:image/jpeg;base64,fixture"
    schema = request["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == sorted(schema["properties"])


@pytest.mark.parametrize(
    "body",
    [
        {"status": "incomplete", "output": []},
        {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
        {"status": "completed", "output": []},
    ],
)
def test_openai_incomplete_and_refusal_never_accept(body):
    assert parse_response(PROFILES["luna"], body).verdict == "unavailable"


def test_cost_is_explicit_estimate():
    assert estimate_cost(PROFILES["baseline"], 6000, 200) == 0.007
    assert estimate_cost(PROFILES["luna"], 6000, 200) == 0.0007
    with pytest.raises(ValueError):
        estimate_cost(PROFILES["luna"], -1, 0)
