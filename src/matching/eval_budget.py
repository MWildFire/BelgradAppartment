"""Durable, fail-closed budget accounting for future evaluation; no provider transport.

All money is integer micro-USD. A caller must establish a conservative token/price
bound BEFORE reserving. This module cannot make an unverified token estimate true.
Unknown billing stays reserved forever until an explicit reconciliation. A lost
process, a retry or reopening the ledger never resets the budget or permits replay.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import uuid4


class BudgetRejected(RuntimeError):
    """No request is allowed; the caller must not fall back to an unguarded path."""


def _integer(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError("Expected a bounded non-negative integer")
    if value > 1_000_000_000_000:
        raise ValueError("Integer outside accounting range")
    return value


@dataclass(frozen=True)
class Quote:
    """Rates in USD / million tokens; micro-USD = tokens * those rates."""

    model: str
    max_input_tokens: int
    max_output_tokens: int
    input_rate: str
    output_rate: str

    def cost(self, input_tokens: int, output_tokens: int) -> int:
        _integer(input_tokens)
        _integer(output_tokens)
        rates = [Decimal(self.input_rate), Decimal(self.output_rate)]
        if any(not r.is_finite() or r <= 0 or r > 1000 for r in rates):
            raise ValueError("Invalid or unsupported price")
        # Fraction(Decimal) is exact and independent of an ambient Decimal context.
        # Bound the rate scale so malformed inputs cannot create enormous integers.
        if any(int(r.as_tuple().exponent) < -9 for r in rates):
            raise ValueError("Price precision exceeds supported scale")
        cost = input_tokens * Fraction(rates[0]) + output_tokens * Fraction(rates[1])
        return (cost.numerator + cost.denominator - 1) // cost.denominator

    def ceiling(self) -> int:
        if not self.model or len(self.model) > 128:
            raise ValueError("Missing model identity")
        _integer(self.max_input_tokens, positive=True)
        _integer(self.max_output_tokens, positive=True)
        return self.cost(self.max_input_tokens, self.max_output_tokens)


@dataclass(frozen=True)
class Reservation:
    attempt_id: str
    token: str


class BudgetLedger:
    """Separate local SQLite ledger; never uses the bot's production database.

    Stage amounts and request counts are immutable once created. Reopening with
    different limits fails, so a restart cannot silently grant a new allowance.
    Canceled-before-dispatch requests still consume the request-count allowance.
    """

    def __init__(
        self,
        path: Path,
        *,
        total_micro_usd: int = 50_000_000,
        stages: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self.path = path
        _integer(total_micro_usd, positive=True)
        stages = (
            stages
            if stages is not None
            else {"pilot": (20_000_000, 1200), "confirmation": (30_000_000, 1600)}
        )
        if not stages:
            raise ValueError("At least one explicit stage required")
        for name, (amount, requests) in stages.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
                raise ValueError("Invalid stage")
            _integer(amount, positive=True)
            _integer(requests, positive=True)
        config = json.dumps({"total": total_micro_usd, "stages": stages}, sort_keys=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS budget_config ("
                "id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL, halted TEXT)"
            )
            con.execute("""CREATE TABLE IF NOT EXISTS budget_attempts (
                attempt_id TEXT PRIMARY KEY, token TEXT NOT NULL, stage TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, model TEXT NOT NULL,
                max_input INTEGER NOT NULL, max_output INTEGER NOT NULL,
                input_rate TEXT NOT NULL, output_rate TEXT NOT NULL,
                reserved INTEGER NOT NULL CHECK(reserved > 0),
                charged INTEGER NOT NULL CHECK(charged >= 0),
                status TEXT NOT NULL CHECK(status IN
                    ('reserved','dispatched','unknown','settled','canceled')),
                input_tokens INTEGER, output_tokens INTEGER)""")
            row = con.execute("SELECT config FROM budget_config WHERE id=1").fetchone()
            if row is None:
                con.execute("INSERT INTO budget_config VALUES (1,?,NULL)", (config,))
            elif row[0] != config:
                raise BudgetRejected("budget_configuration_changed")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA synchronous=FULL")
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def reserve(
        self, attempt_id: str, stage: str, payload_sha256: str, quote: Quote
    ) -> Reservation:
        ceiling = quote.ceiling()
        if not re.fullmatch(r"[a-zA-Z0-9:_-]{1,160}", attempt_id):
            raise ValueError("Invalid attempt ID")
        if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
            raise ValueError("Expected exact request SHA256")
        token = uuid4().hex
        with self._transaction() as con:
            row = con.execute("SELECT config,halted FROM budget_config WHERE id=1").fetchone()
            assert row is not None
            config = json.loads(row["config"])
            if row["halted"]:
                raise BudgetRejected("budget_halted")
            if stage not in config["stages"]:
                raise BudgetRejected("unapproved_stage")
            if con.execute(
                "SELECT 1 FROM budget_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone():
                raise BudgetRejected("attempt_already_reserved")
            total = con.execute("SELECT COALESCE(SUM(charged),0) FROM budget_attempts").fetchone()[
                0
            ]
            stage_spend, count = con.execute(
                "SELECT COALESCE(SUM(charged),0),COUNT(*) FROM budget_attempts WHERE stage=?",
                (stage,),
            ).fetchone()
            stage_cap, request_cap = config["stages"][stage]
            if total + ceiling > config["total"] or stage_spend + ceiling > stage_cap:
                raise BudgetRejected("cost_limit")
            if count >= request_cap:
                raise BudgetRejected("request_limit")
            con.execute(
                "INSERT INTO budget_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,'reserved',NULL,NULL)",
                (
                    attempt_id,
                    token,
                    stage,
                    payload_sha256,
                    quote.model,
                    quote.max_input_tokens,
                    quote.max_output_tokens,
                    quote.input_rate,
                    quote.output_rate,
                    ceiling,
                    ceiling,
                ),
            )
        return Reservation(attempt_id, token)

    def _transition(
        self, reservation: Reservation, before: str, after: str, *, cancel: bool = False
    ) -> None:
        with self._transaction() as con:
            halted = con.execute("SELECT halted FROM budget_config WHERE id=1").fetchone()[0]
            if after == "dispatched" and halted:
                raise BudgetRejected("budget_halted")
            result = con.execute(
                "UPDATE budget_attempts SET status=?, charged=CASE WHEN ? THEN 0 ELSE charged END "
                "WHERE attempt_id=? AND token=? AND status=?",
                (after, cancel, reservation.attempt_id, reservation.token, before),
            )
            if result.rowcount != 1:
                raise BudgetRejected("invalid_attempt_transition")

    def dispatch(self, reservation: Reservation) -> None:
        """Must durably succeed immediately BEFORE a transport is allowed to send."""
        self._transition(reservation, "reserved", "dispatched")

    def cancel_unsent(self, reservation: Reservation) -> None:
        """Only a definitely unsent reservation may be refunded without billing evidence."""
        self._transition(reservation, "reserved", "canceled", cancel=True)

    def unknown(self, reservation: Reservation) -> None:
        self._transition(reservation, "dispatched", "unknown")

    def unpriced(self, reservation: Reservation) -> None:
        """An unsupported billing dimension invalidates the quote; hold and halt."""
        with self._transaction() as con:
            result = con.execute(
                "UPDATE budget_attempts SET status='unknown' "
                "WHERE attempt_id=? AND token=? AND status='dispatched'",
                (reservation.attempt_id, reservation.token),
            )
            if result.rowcount != 1:
                raise BudgetRejected("invalid_attempt_transition")
            con.execute("UPDATE budget_config SET halted='unpriced_usage' WHERE id=1")

    def settle(
        self, reservation: Reservation, *, model: str, input_tokens: int, output_tokens: int
    ) -> None:
        _integer(input_tokens)
        _integer(output_tokens)
        breach = False
        with self._transaction() as con:
            row = con.execute(
                "SELECT * FROM budget_attempts WHERE attempt_id=? AND token=?",
                (reservation.attempt_id, reservation.token),
            ).fetchone()
            if row is None or row["status"] not in {"dispatched", "unknown"}:
                raise BudgetRejected("invalid_attempt_transition")
            if model != row["model"]:
                # Unknown rate/model: keep the full reserve and stop all future sends.
                con.execute(
                    "UPDATE budget_attempts SET status='unknown' WHERE attempt_id=?",
                    (reservation.attempt_id,),
                )
                breach = True
            else:
                quote = Quote(
                    model,
                    row["max_input"],
                    row["max_output"],
                    row["input_rate"],
                    row["output_rate"],
                )
                actual = quote.cost(input_tokens, output_tokens)
                breach = (
                    input_tokens > quote.max_input_tokens or output_tokens > quote.max_output_tokens
                )
                con.execute(
                    "UPDATE budget_attempts SET status='settled',charged=?,"
                    "input_tokens=?,output_tokens=? "
                    "WHERE attempt_id=?",
                    (actual, input_tokens, output_tokens, reservation.attempt_id),
                )
            if breach:
                con.execute("UPDATE budget_config SET halted='quote_breach' WHERE id=1")
        # Persist the accounting and circuit BEFORE raising: rollback must not erase a breach.
        if breach:
            raise BudgetRejected("quote_breach")

    def snapshot(self) -> dict[str, Any]:
        with self._transaction() as con:
            config = con.execute("SELECT config,halted FROM budget_config WHERE id=1").fetchone()
            rows = con.execute(
                "SELECT stage,status,COUNT(*) AS attempts,SUM(charged) AS charged "
                "FROM budget_attempts GROUP BY stage,status"
            ).fetchall()
            return {
                "config": json.loads(config["config"]),
                "halted": config["halted"],
                "accounted_micro_usd": sum(row["charged"] for row in rows),
                "groups": [dict(row) for row in rows],
            }
