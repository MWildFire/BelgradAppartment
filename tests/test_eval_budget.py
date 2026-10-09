from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import InvalidOperation, localcontext

import pytest

from src.matching.eval_budget import BudgetLedger, BudgetRejected, Quote, Reservation
from src.matching.guarded_eval import evaluate_fixture
from src.matching.model_adapters import PROFILES, fixture_response

SHA = "a" * 64
QUOTE = Quote("model", 6, 4, "1", "1")  # exactly 10 micro-USD


def ledger(path, cap=20, count=10):
    return BudgetLedger(path, total_micro_usd=cap, stages={"pilot": (cap, count)})


def test_unknown_and_crashed_attempts_never_refund_on_reopen(tmp_path):
    path = tmp_path / "budget.db"
    b = ledger(path)
    a = b.reserve("a", "pilot", SHA, QUOTE)
    b.dispatch(a)
    b.unknown(a)
    b.reserve("crashed-before-send", "pilot", SHA, QUOTE)
    b = ledger(path)
    assert b.snapshot()["accounted_micro_usd"] == 20
    with pytest.raises(BudgetRejected, match="cost_limit"):
        b.reserve("c", "pilot", SHA, QUOTE)
    with pytest.raises(BudgetRejected, match="configuration_changed"):
        ledger(path, cap=30)
    with pytest.raises(BudgetRejected):
        b.cancel_unsent(a)


def test_request_replay_token_and_double_settlement_rejected(tmp_path):
    b = ledger(tmp_path / "budget.db")
    a = b.reserve("a", "pilot", SHA, QUOTE)
    with pytest.raises(BudgetRejected, match="already_reserved"):
        b.reserve("a", "pilot", SHA, QUOTE)
    with pytest.raises(BudgetRejected):
        b.dispatch(Reservation(a.attempt_id, "forged"))
    b.dispatch(a)
    with pytest.raises(BudgetRejected):
        b.dispatch(a)
    b.settle(a, model="model", input_tokens=2, output_tokens=1)
    assert b.snapshot()["accounted_micro_usd"] == 3
    with pytest.raises(BudgetRejected):
        b.settle(a, model="model", input_tokens=0, output_tokens=0)


def test_stage_total_and_request_caps_are_separate(tmp_path):
    b = BudgetLedger(
        tmp_path / "budget.db",
        total_micro_usd=25,
        stages={"pilot": (10, 1), "confirmation": (30, 3)},
    )
    a = b.reserve("a", "pilot", SHA, QUOTE)
    b.cancel_unsent(a)
    with pytest.raises(BudgetRejected, match="request_limit"):
        b.reserve("b", "pilot", SHA, QUOTE)
    for name in ("c", "d"):
        b.reserve(name, "confirmation", SHA, QUOTE)
    with pytest.raises(BudgetRejected, match="cost_limit"):
        b.reserve("e", "confirmation", SHA, QUOTE)
    with pytest.raises(BudgetRejected, match="unapproved_stage"):
        b.reserve("f", "unapproved", SHA, QUOTE)


@pytest.mark.parametrize(
    "model,input_tokens,output_tokens", [("model", 7, 4), ("changed-model", 6, 4)]
)
def test_unexpected_billing_halts_persistently_before_any_future_dispatch(
    tmp_path, model, input_tokens, output_tokens
):
    path = tmp_path / "budget.db"
    b = ledger(path, cap=30)
    a = b.reserve("a", "pilot", SHA, QUOTE)
    reserved_other = b.reserve("b", "pilot", SHA, QUOTE)
    b.dispatch(a)
    with pytest.raises(BudgetRejected, match="quote_breach"):
        b.settle(a, model=model, input_tokens=input_tokens, output_tokens=output_tokens)
    b = ledger(path, cap=30)
    assert b.snapshot()["halted"] == "quote_breach"
    with pytest.raises(BudgetRejected, match="budget_halted"):
        b.dispatch(reserved_other)
    with pytest.raises(BudgetRejected, match="budget_halted"):
        b.reserve("c", "pilot", SHA, QUOTE)


def test_concurrent_processes_cannot_overspend(tmp_path):
    path = tmp_path / "budget.db"
    ledger(path, cap=30)
    script = """
import sys
from pathlib import Path
from src.matching.eval_budget import BudgetLedger, BudgetRejected, Quote
b=BudgetLedger(Path(sys.argv[1]),total_micro_usd=30,stages={'pilot':(30,10)})
try:
 b.reserve(sys.argv[2],'pilot','a'*64,Quote('model',6,4,'1','1'))
 print('reserved')
except BudgetRejected:
 print('denied')
"""

    def process(i):
        return subprocess.check_output(
            [sys.executable, "-c", script, str(path), str(i)], text=True
        ).strip()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(process, range(8)))
    assert results.count("reserved") == 3 and results.count("denied") == 5
    assert ledger(path, cap=30).snapshot()["accounted_micro_usd"] == 30


@pytest.mark.parametrize("rate", ["NaN", "Infinity", "-1", "0", "bad"])
def test_invalid_prices_and_rounding(rate):
    with pytest.raises((ValueError, InvalidOperation)):
        Quote("m", 1, 1, rate, "1").ceiling()
    assert Quote("m", 1, 1, "0.1", "0.1").ceiling() == 1
    with pytest.raises(ValueError):
        Quote("m", True, 1, "1", "1").ceiling()


def test_offline_adapter_accounts_refusal_and_preserves_unknown_billing(tmp_path):
    b = ledger(tmp_path / "budget.db", cap=1600)
    profile = PROFILES["luna"]
    response = fixture_response(
        profile,
        {
            "is_same_apartment": True,
            "confidence": 0.99,
            "uncertain": False,
            "reasoning": "synthetic",
        },
    )
    args = {
        "ledger": b,
        "stage": "pilot",
        "profile": profile,
        "content": [{"type": "text", "text": "synthetic"}],
        "response": response,
        "max_input_tokens": 6000,
    }
    # Reserve800, timeout keeps800; second reserve800 settles700 even if response invalid.
    result = evaluate_fixture(attempt_id="timeout", fault="timeout", **args)
    assert result.actual_api_calls == 0
    response["status"] = "incomplete"
    result = evaluate_fixture(attempt_id="refusal", **args)
    assert result.outcome.verdict == "unavailable" and result.actual_api_calls == 0
    assert b.snapshot()["accounted_micro_usd"] == 1500
    with pytest.raises(BudgetRejected, match="cost_limit"):
        evaluate_fixture(attempt_id="denied", **args)
    assert "synthetic" not in json.dumps(b.snapshot())


def test_quote_ignores_ambient_decimal_precision():
    with localcontext() as context:
        context.prec = 2
        assert Quote("m", 99, 1, "1.05", "0.01").ceiling() == 104


@pytest.mark.parametrize(
    "extra", [{"cache_creation_input_tokens": 6000}, {"new_billable_dimension": 1}]
)
def test_partial_usage_does_not_refund_unpriced_dimensions(tmp_path, extra):
    b = BudgetLedger(tmp_path / "budget.db")
    profile = PROFILES["baseline"]
    response = fixture_response(
        profile,
        {"is_same_apartment": True, "confidence": 0.99, "uncertain": False, "reasoning": "fixture"},
    )
    response["usage"] = {"input_tokens": 0, "output_tokens": 0, **extra}
    result = evaluate_fixture(
        b,
        attempt_id="partial",
        stage="pilot",
        profile=profile,
        content=[],
        response=response,
        max_input_tokens=6000,
    )
    assert result.outcome.reason_code == "unpriced_usage"
    assert b.snapshot()["accounted_micro_usd"] == 8000
    assert b.snapshot()["halted"] == "unpriced_usage"
