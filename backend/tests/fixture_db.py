"""Fixture risk dbs built with the REAL writers.

The observer is tested against the schemas the engine actually produces —
PaperStore, TradingStateMachine, SupervisionStore, AlertOutbox — not a
hand-rolled imitation that would drift the day a column changes. That claim
extends to audit payloads: reconcile rows come out of the real
ReconcileEngine (double-audited exactly the way reconcile_and_arm does),
verdict rows out of the real RiskGovernor running real checks, and order/fill
rows carry the same keys paper.py writes. nwt_risk is a test-only import: the
backend package itself never touches it, and the import-linter contract
enforces that.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from nwt_contracts import (
    AssetClass,
    OrderIntent,
    PortfolioView,
    RiskContext,
    Side,
    TradingState,
)
from nwt_engine.broker import AccountState, Broker, BrokerPosition, OrderStatus
from nwt_engine.data import ParquetStore
from nwt_engine.domain import Bar, Timeframe
from nwt_engine.sleeves import LedgerEntry, SleeveLedger
from nwt_risk.alerts import AlertOutbox
from nwt_risk.checks import OrderSizeCheck, StateGateCheck
from nwt_risk.config import ReconcileRules, RiskConfig, SleeveBudget
from nwt_risk.context import GovernorContext
from nwt_risk.governor import RiskGovernor
from nwt_risk.paper import PaperStore
from nwt_risk.reconcile import ExpectedState, ReconcileEngine
from nwt_risk.state import TradingStateMachine
from nwt_risk.supervision import SupervisionStore

NOW = datetime(2026, 8, 5, 18, 0, tzinfo=UTC)

_CONFIG_TEMPLATE = """\
universe_files: []
data_root: {data_root}
data_provider: alpaca
sleeves:
  - sleeve_id: control
    strategy: buyhold
    capital: "2500"
    params: {{}}
  - sleeve_id: momentum
    strategy: momentum_rotation
    capital: "2000"
    params: {{}}
"""


def _bar(symbol: str, close: str, ts_close: datetime) -> Bar:
    price = Decimal(close)
    return Bar(
        symbol=symbol,
        timeframe=Timeframe.D1,
        ts_open=ts_close - timedelta(hours=7),
        ts_close=ts_close,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1000"),
    )


class _MirrorBroker(Broker):
    """Answers reconciliation with exactly the book it was handed: the
    fixture's reconcile passes are real ReconcileEngine runs that verify."""

    def __init__(self, cash: Decimal, positions: dict[str, tuple[Decimal, Decimal]]):
        self._cash = cash
        self._positions = positions

    def get_account(self) -> AccountState:
        return AccountState(ts=NOW, cash=self._cash, equity=self._cash)

    def get_positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(symbol=symbol, qty=qty, avg_cost=avg)
            for symbol, (qty, avg) in sorted(self._positions.items())
            if qty != 0
        ]

    def get_open_orders(self) -> list[OrderStatus]:
        return []

    def submit(self, ticket):  # pragma: no cover - reconcile never submits
        raise NotImplementedError

    def cancel(self, client_order_id):  # pragma: no cover
        raise NotImplementedError

    def cancel_all(self):  # pragma: no cover
        raise NotImplementedError

    def drain_events(self):  # pragma: no cover
        raise NotImplementedError


def _reconcile_pass(
    store: PaperStore, scratch: dict[str, SleeveLedger], when: datetime
) -> None:
    """One real reconcile pass, audited TWICE like the engine does: once
    inside ReconcileEngine.reconcile (wired to store.audit exactly as
    paper.py wires it) and once more the way reconcile_and_arm re-audits the
    same report. Two rows, one pass — the shape the observer must not
    double-count."""
    cash = sum((ledger.cash for ledger in scratch.values()), Decimal("0"))
    positions: dict[str, tuple[Decimal, Decimal]] = {}
    for ledger in scratch.values():
        for symbol, (qty, avg) in ledger.positions.items():
            if qty != 0:
                held, _ = positions.get(symbol, (Decimal("0"), Decimal("0")))
                positions[symbol] = (held + qty, avg)
    expected = ExpectedState(
        cash=cash,
        positions={s: q for s, (q, _a) in positions.items()},
        open_client_order_ids=set(),
    )
    engine = ReconcileEngine(
        _MirrorBroker(cash, positions),
        ReconcileRules(),
        lambda: when,
        audit=lambda kind, payload: store.audit(when, kind, payload),
    )
    report = engine.reconcile(expected)
    assert report.ok, f"fixture reconcile must verify: {report.unexplained}"
    store.audit(when, "reconcile", report.model_dump(mode="json"))


def _governor_verdict(store: PaperStore, when: datetime) -> None:
    """A real rejected verdict from the real governor running real checks in
    the fixture's real state (HALTED): the audit payload is whatever the
    governor writes, so a key rename there breaks these fixtures loudly."""
    cfg = RiskConfig(
        sleeves={
            "momentum": SleeveBudget(
                budget_usd=Decimal("2000"), max_order_usd=Decimal("500")
            )
        },
        config_hash="cafe",
    )
    governor = RiskGovernor(
        [StateGateCheck(), OrderSizeCheck()],
        cfg,
        lambda: TradingState.HALTED,
        audit=lambda kind, payload: store.audit(when, kind, payload),
    )
    intent = OrderIntent(
        intent_id="intent-1",
        sleeve_id="momentum",
        strategy="momentum_rotation",
        symbol="SPY",
        asset_class=AssetClass.ETF,
        side=Side.BUY,
        qty=Decimal("1"),
        limit_price=Decimal("769.25"),  # one share exceeds the 500 notional cap
        as_of=when,
        created_at=when,
    )
    ctx = GovernorContext(
        base=RiskContext(
            ts=when,
            mode="paper",
            trading_state=TradingState.HALTED,
            account=PortfolioView(
                scope="account", ts=when, cash=Decimal("2000"), equity=Decimal("2000")
            ),
        )
    )
    outcome = governor.review([intent], ctx)
    assert not outcome.approved and outcome.verdicts[0].decision == "reject"


def build_fixture(
    root: Path,
    *,
    symbols_with_marks: tuple[str, ...] = ("SPY", "EEM"),
    with_fill_audit: bool = True,
    extra_sleeve_entries: tuple[tuple[str, LedgerEntry], ...] = (),
) -> tuple[Path, Path]:
    """Seed <root>/data/risk.db + config; returns (db_path, config_path).

    The book it builds: control bought 1 SPY @ 769.25 yesterday; momentum
    bought 15 EEM @ 65.88 today. Marks: SPY 771.00, EEM 64.00 (when written).
    Reconciles: one pass yesterday, two today — each written as the engine's
    two audit rows per pass.
    """
    db = root / "data" / "risk.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    yesterday_fill = NOW - timedelta(days=1, hours=2)
    today_fill = NOW - timedelta(hours=2)

    machine = TradingStateMachine(db, "paper", lambda: NOW - timedelta(hours=2))
    machine.on_startup()  # HALTED behind an un-acked startup latch, age 2h

    store = PaperStore(db)
    scratch = {
        "control": SleeveLedger("control", Decimal("2500")),
        "momentum": SleeveLedger("momentum", Decimal("2000")),
    }

    store.record_order(
        "coid-spy", "control", None, "SPY", Side.BUY,
        Decimal("1"), None, None, yesterday_fill, True,
    )
    store.mark_order("coid-spy", "filled")
    store.apply_entry(
        "control",
        LedgerEntry(
            kind="fill", ts=yesterday_fill, symbol="SPY", side=Side.BUY,
            qty=Decimal("1"), price=Decimal("769.25"), fees=Decimal("0"),
        ),
        scratch["control"],
    )
    # Same payload keys _submit and poll_fills write (paper.py).
    store.audit(
        yesterday_fill,
        "order",
        {"coid": "coid-spy", "state": "accepted", "reason": None, "symbol": "SPY"},
    )
    store.audit(yesterday_fill, "fill", {"fill_id": "f1", "coid": "coid-spy"})
    _reconcile_pass(store, scratch, yesterday_fill)

    store.record_order(
        "coid-eem", "momentum", None, "EEM", Side.BUY,
        Decimal("15"), None, None, today_fill, True,
    )
    store.mark_order("coid-eem", "filled")
    store.apply_entry(
        "momentum",
        LedgerEntry(
            kind="fill", ts=today_fill, symbol="EEM", side=Side.BUY,
            qty=Decimal("15"), price=Decimal("65.88"), fees=Decimal("0"),
        ),
        scratch["momentum"],
    )
    for sleeve_id, entry in extra_sleeve_entries:
        ledger = scratch.setdefault(sleeve_id, SleeveLedger(sleeve_id, Decimal("1000000")))
        store.apply_entry(sleeve_id, entry, ledger)
    store.audit(
        today_fill,
        "order",
        {"coid": "coid-eem", "state": "accepted", "reason": None, "symbol": "EEM"},
    )
    if with_fill_audit:
        store.audit(today_fill, "fill", {"fill_id": "f2", "coid": "coid-eem"})
    _reconcile_pass(store, scratch, today_fill)
    _reconcile_pass(store, scratch, today_fill + timedelta(minutes=30))
    _governor_verdict(store, today_fill)

    store.day_open("2026-08-04", Decimal("10000"))
    store.day_open("2026-08-05", Decimal("10001.5"))

    SupervisionStore(db).beat(
        NOW - timedelta(minutes=5), NOW + timedelta(minutes=5), "poll", "next poll due"
    )

    outbox = AlertOutbox(db, lambda: NOW - timedelta(hours=1))
    outbox.raise_alert(
        "CRITICAL", "scheduler", "reconcile mismatch — HALTED", {"detail": "fixture"}
    )

    parquet = ParquetStore(root / "data" / "parquet")
    closes = {"SPY": "771.00", "EEM": "64.00"}
    for symbol in symbols_with_marks:
        parquet.write_bars(
            "alpaca", Timeframe.D1, symbol,
            [_bar(symbol, closes[symbol], NOW - timedelta(hours=4))],
        )

    config = root / "config" / "paper.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(_CONFIG_TEMPLATE.format(data_root=root / "data" / "parquet"))
    return db, config
