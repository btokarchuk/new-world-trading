"""End-to-end attended paper cycle against the stateful FakeAlpaca:
reconcile -> arm -> decide -> govern -> submit -> fill -> ledger -> re-reconcile.

This is the Phase 3 milestone in test form: the exact stack `nwt-risk cycle`
assembles, minus the network.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fake_alpaca import FakeAlpaca

from nwt_contracts import TradingState
from nwt_engine.broker.alpaca import AlpacaHttpBroker
from nwt_engine.data.fixtures import generate_synthetic
from nwt_engine.domain import Instrument, Universe
from nwt_risk.breakers import CircuitBreakers
from nwt_risk.checks import default_checks
from nwt_risk.config import RiskConfig
from nwt_risk.governor import RiskGovernor
from nwt_risk.paper import PaperCycle, PaperStore, SleeveSpec
from nwt_risk.reconcile import ReconcileEngine
from nwt_risk.state import TradingStateMachine

# Fixed decision time: 10:30 ET on a summer weekday (inside RTH, before cutoff).
_NOW = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)


@pytest.fixture()
def stack(tmp_path: Path):
    """A builder that assembles a FRESH stack on the shared db per call —
    faithful to `nwt-risk cycle`, where every invocation is a new process
    (on_startup -> reconcile -> paper auto-resume) and durable state lives
    only in SQLite and at the broker."""
    fake = FakeAlpaca(cash="10000", base_ts=_NOW)
    cfg = RiskConfig.load(Path(__file__).parents[2] / "config" / "risk.yaml")
    db = tmp_path / "paper.db"
    now_fn = lambda: _NOW  # noqa: E731
    bars, _ = generate_synthetic(symbol="SPY", seed=5)
    # Quote coherent with the bar tape: live price = last synthetic close.
    last_close = bars[-1].close
    quote = {
        "SPY": {
            "ts": _NOW - timedelta(seconds=2),
            "bid": last_close - Decimal("0.10"),
            "ask": last_close + Decimal("0.10"),
            "last": last_close,
        }
    }

    def build() -> tuple[TradingStateMachine, PaperCycle]:
        broker = AlpacaHttpBroker(
            "https://paper-api.alpaca.markets", "key", "secret", client=fake.client()
        )
        state = TradingStateMachine(db, "paper", now_fn)
        state.on_startup()
        cycle = PaperCycle(
            broker=broker,
            universe=Universe(
                name="t",
                instruments=(
                    Instrument(symbol="SPY", asset_class="etf", calendar="XNYS"),
                ),
            ),
            sleeves=[
                SleeveSpec(
                    sleeve_id="control",
                    strategy="buyhold",
                    capital=Decimal("2500"),
                    params={"symbol": "SPY"},
                )
            ],
            risk_config=cfg,
            store=PaperStore(db),
            state_machine=state,
            breakers=CircuitBreakers(cfg.breakers, state, now_fn, db),
            governor=RiskGovernor(default_checks(), cfg, state.state),
            reconciler=ReconcileEngine(broker, cfg.reconcile, now_fn, lambda k, p: None),
            bars_loader=lambda: {"SPY": bars},
            quotes_loader=lambda: quote,
            now_fn=now_fn,
            unallocated_buffer=Decimal("7500"),
        )
        return state, cycle

    return fake, build


def _arm(state: TradingStateMachine) -> None:
    record = state.current()
    result = state.request_transition(
        TradingState.ACTIVE,
        actor="test",
        confirmation="RESUME paper",
        acked_latch_ids=[latch.latch_id for latch in record.latches if not latch.acked],
    )
    assert result.ok, result.error


def test_attended_cycles_end_to_end(stack):
    fake, build = stack

    # Process 1 — fresh deployment: reconcile passes, state HALTED, no orders.
    state, cycle = build()
    report = cycle.run_cycle()
    assert report.reconciled
    assert report.state is TradingState.HALTED
    assert report.submitted == 0
    _arm(state)  # the one-time attended arming step

    # Process 2 — armed: buyhold proposes, governor approves, order submitted.
    state, cycle = build()
    report = cycle.run_cycle()
    assert report.state is TradingState.ACTIVE
    assert report.proposals == 1
    assert report.approved >= 1
    assert report.submitted == 1
    assert len(fake.orders) == 1
    order = fake.orders[0]
    assert order["symbol"] == "SPY"
    assert order["side"] == "buy"
    # The $1,000 order-notional cap over a ~$100.50 limit bounds this at 9
    # shares; the sleeve wants far more, so the clamp is what is under test.
    assert Decimal(order["qty"]) <= 9
    assert Decimal(order["qty"]) * Decimal(order["limit_price"]) <= Decimal("1000")

    # Process 3 — same day re-run: deterministic coid + target diffing means
    # no duplicate order reaches the broker.
    state, cycle = build()
    cycle.run_cycle()
    assert len(fake.orders) == 1

    # Fill lands; process 4 polls it into the ledger and the books balance
    # (poll uses reconcile_and_arm — a verified reconcile counts everywhere).
    fake.fill(order["id"], Decimal(order["limit_price"]))
    state, cycle = build()
    ledgers = cycle.store.fold_ledgers(cycle.sleeve_specs)
    applied = cycle.poll_fills(ledgers)
    assert applied == 1
    assert ledgers["control"].position_qty("SPY") == Decimal(order["qty"])
    assert cycle.reconcile_and_arm(ledgers)

    # Process 5 — full cycle after the fill: reconcile clean, auto-resume,
    # no new orders needed (target already held).
    state, cycle = build()
    report = cycle.run_cycle()
    assert report.reconciled
    assert report.state is TradingState.ACTIVE
    assert len(fake.orders) == 1


def test_tampered_broker_position_halts(stack):
    fake, build = stack
    state, cycle = build()
    cycle.run_cycle()
    _arm(state)

    # Someone (or something) creates a position the ledgers know nothing about.
    fake.positions["QQQ"] = {"qty": Decimal("10"), "avg_entry_price": Decimal("400")}
    state, cycle = build()
    report = cycle.run_cycle()
    assert not report.reconciled
    assert report.state is TradingState.HALTED
    assert report.submitted == 0
