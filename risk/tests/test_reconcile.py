from datetime import UTC, datetime
from decimal import Decimal

from nwt_engine.broker import AccountState, BrokerPosition, OrderStatus
from nwt_engine.domain import OrderState

from nwt_risk.config import ReconcileRules
from nwt_risk.reconcile import ExpectedState, PositionDiff, ReconcileEngine

NOW = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)


class ScriptedBroker:
    """Preset broker truth; mutating methods raise to prove reconcile is read-only."""

    def __init__(self, cash: str = "1000", positions=(), open_ids=()) -> None:
        self._cash = Decimal(cash)
        self._positions = [
            BrokerPosition(symbol=symbol, qty=Decimal(qty), avg_cost=Decimal("1"))
            for symbol, qty in positions
        ]
        self._open = [
            OrderStatus(
                client_order_id=coid, state=OrderState.ACKED, filled_qty=Decimal("0"), ts=NOW
            )
            for coid in open_ids
        ]

    def get_account(self) -> AccountState:
        return AccountState(ts=NOW, cash=self._cash, equity=self._cash)

    def get_positions(self) -> list[BrokerPosition]:
        return list(self._positions)

    def get_open_orders(self) -> list[OrderStatus]:
        return list(self._open)

    def submit(self, ticket):
        raise AssertionError("reconcile must never submit orders")

    def cancel(self, client_order_id):
        raise AssertionError("reconcile must never cancel orders")

    def cancel_all(self):
        raise AssertionError("reconcile must never cancel orders")

    def drain_events(self):
        raise AssertionError("reconcile must never drain events")


def _engine(broker: ScriptedBroker) -> tuple[ReconcileEngine, list[tuple[str, dict]]]:
    audit_calls: list[tuple[str, dict]] = []
    engine = ReconcileEngine(
        broker,
        ReconcileRules(),
        lambda: NOW,
        lambda kind, payload: audit_calls.append((kind, payload)),
    )
    return engine, audit_calls


def _expected(cash: str = "1000", positions=None, open_ids=()) -> ExpectedState:
    return ExpectedState(
        cash=Decimal(cash),
        positions={symbol: Decimal(qty) for symbol, qty in (positions or {}).items()},
        open_client_order_ids=set(open_ids),
    )


def test_clean_pass_ok_and_audited():
    broker = ScriptedBroker(cash="1000", positions=[("AAPL", "10")], open_ids=["c-1"])
    engine, audit_calls = _engine(broker)

    report = engine.reconcile(_expected(positions={"AAPL": "10"}, open_ids=["c-1"]))

    assert report.ok is True
    assert report.ts == NOW
    assert report.cash_diff == Decimal("0")
    assert report.position_diffs == ()
    assert report.external_order_ids == ()
    assert report.unexplained == ()
    (call,) = audit_calls
    assert call[0] == "reconcile"
    assert call[1]["ok"] is True


def test_cash_within_tolerance_ok():
    broker = ScriptedBroker(cash="1004.99")
    engine, _ = _engine(broker)

    report = engine.reconcile(_expected(cash="1000"))

    assert report.ok is True
    assert report.cash_diff == Decimal("4.99")
    assert report.unexplained == ()


def test_cash_beyond_tolerance_not_ok():
    broker = ScriptedBroker(cash="1005.01")
    engine, audit_calls = _engine(broker)

    report = engine.reconcile(_expected(cash="1000"))

    assert report.ok is False
    assert report.cash_diff == Decimal("5.01")
    assert any("cash" in line for line in report.unexplained)
    assert audit_calls[0][1]["ok"] is False  # audited every run, including failures


def test_one_share_equity_drift_not_ok():
    broker = ScriptedBroker(positions=[("AAPL", "11")])
    engine, _ = _engine(broker)

    report = engine.reconcile(_expected(positions={"AAPL": "10"}))

    assert report.ok is False
    assert report.position_diffs == (
        PositionDiff(symbol="AAPL", expected=Decimal("10"), actual=Decimal("11")),
    )
    assert any("AAPL" in line for line in report.unexplained)


def test_crypto_dust_within_relative_tolerance_ok():
    broker = ScriptedBroker(positions=[("BTC/USD", "0.5000000001")])
    engine, _ = _engine(broker)

    report = engine.reconcile(_expected(positions={"BTC/USD": "0.5"}))

    assert report.ok is True
    assert len(report.position_diffs) == 1  # dust stays visible, just tolerated
    assert report.unexplained == ()


def test_crypto_beyond_relative_tolerance_not_ok():
    broker = ScriptedBroker(positions=[("BTC/USD", "0.51")])
    engine, _ = _engine(broker)

    report = engine.reconcile(_expected(positions={"BTC/USD": "0.5"}))

    assert report.ok is False
    assert any("BTC/USD" in line for line in report.unexplained)


def test_external_order_reported_but_ok_stays_true():
    broker = ScriptedBroker(open_ids=["mystery-1"])
    engine, _ = _engine(broker)

    report = engine.reconcile(_expected())

    assert report.external_order_ids == ("mystery-1",)
    assert report.ok is True  # external is WARN-class, not a mismatch
    assert report.unexplained == ()


def test_in_flight_ids_suppress_external_classification():
    broker = ScriptedBroker(open_ids=["just-sub-1"])
    engine, _ = _engine(broker)

    report = engine.reconcile(_expected(), in_flight_ids={"just-sub-1"})

    assert report.external_order_ids == ()
    assert report.ok is True
