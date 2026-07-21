"""Reconciliation: broker truth versus ledger expectation.

Read-only by design — this engine reports drift and external orders; it
NEVER submits, cancels, or "fixes" anything. A mismatch beyond tolerance
(ok=False) is the runtime's cue to HALT via RECONCILE_MISMATCH; external
orders are WARN-class (EXTERNAL_ORDER) and never flip ok on their own.
"""

from datetime import datetime
from decimal import Decimal
from typing import Callable

from pydantic import BaseModel

from nwt_engine.broker import Broker

from .config import ReconcileRules


class ExpectedState(BaseModel, frozen=True):
    """Built by the runtime from sleeve ledgers."""

    cash: Decimal
    positions: dict[str, Decimal]
    open_client_order_ids: set[str]


class PositionDiff(BaseModel, frozen=True):
    symbol: str
    expected: Decimal
    actual: Decimal


class ReconcileReport(BaseModel, frozen=True):
    ok: bool
    ts: datetime
    cash_diff: Decimal
    position_diffs: tuple[PositionDiff, ...]
    external_order_ids: tuple[str, ...]
    unexplained: tuple[str, ...]


class ReconcileEngine:
    def __init__(
        self,
        broker: Broker,
        rules: ReconcileRules,
        now_fn: Callable[[], datetime],
        audit: Callable[[str, dict], None],
    ) -> None:
        self._broker = broker
        self._rules = rules
        self._now = now_fn
        self._audit = audit

    def reconcile(
        self, expected: ExpectedState, in_flight_ids: set[str] = frozenset()
    ) -> ReconcileReport:
        account = self._broker.get_account()
        actual_positions = {p.symbol: p.qty for p in self._broker.get_positions()}
        open_orders = self._broker.get_open_orders()

        unexplained: list[str] = []

        cash_diff = account.cash - expected.cash
        if abs(cash_diff) > self._rules.cash_tolerance_usd:
            unexplained.append(
                f"cash: expected {expected.cash} actual {account.cash} "
                f"(diff {cash_diff}, tolerance {self._rules.cash_tolerance_usd})"
            )

        diffs: list[PositionDiff] = []
        for symbol in sorted(set(expected.positions) | set(actual_positions)):
            exp = expected.positions.get(symbol, Decimal("0"))
            act = actual_positions.get(symbol, Decimal("0"))
            if act == exp:
                continue
            diffs.append(PositionDiff(symbol=symbol, expected=exp, actual=act))
            if "/" in symbol:
                # Crypto: fee/rounding dust is expected; relative tolerance.
                denominator = max(abs(exp), abs(act))
                if abs(act - exp) <= self._rules.crypto_qty_rel_tolerance * denominator:
                    continue
                unexplained.append(f"crypto qty {symbol}: expected {exp} actual {act}")
            else:
                # Equities are whole intent: ZERO tolerance.
                unexplained.append(f"equity qty {symbol}: expected {exp} actual {act}")

        known_ids = expected.open_client_order_ids | set(in_flight_ids)
        external = tuple(
            sorted(o.client_order_id for o in open_orders if o.client_order_id not in known_ids)
        )

        report = ReconcileReport(
            ok=not unexplained,
            ts=self._now(),
            cash_diff=cash_diff,
            position_diffs=tuple(diffs),
            external_order_ids=external,
            unexplained=tuple(unexplained),
        )
        self._audit("reconcile", report.model_dump(mode="json"))
        return report
