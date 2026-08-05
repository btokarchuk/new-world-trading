"""The protection reconciler: desired coverage minus resting equals action.

The reconciler is the answer to an observed failure (2026-08-05 06:12: the
watchdog's cancel_all stripped a resting stop and nothing re-armed it), so
these tests are phrased as the situations that strip or drift coverage, not as
method-by-method unit checks.
"""

from datetime import UTC, datetime
from decimal import Decimal

from nwt_contracts import OrderRef, Side
from nwt_engine.domain import Instrument, Universe
from nwt_engine.sleeves import SleeveLedger

from nwt_risk.config import RiskConfig
from nwt_risk.protect import (
    deterministic_protective_coid,
    is_protective_coid,
    plan_protection,
)

D = Decimal
NOW = datetime(2026, 8, 5, 15, 0, tzinfo=UTC)

UNIVERSE = Universe(
    name="test",
    instruments=(
        Instrument(symbol="SPY", asset_class="etf", calendar="XNYS"),
        Instrument(symbol="IWM", asset_class="etf", calendar="XNYS"),
        Instrument(
            symbol="BTC/USD",
            asset_class="crypto",
            calendar="24_7",
            fractionable=True,
            qty_increment=D("0.0001"),
            tif="gtc",
        ),
    ),
)

CFG = RiskConfig.model_validate(
    {"protection": {"exempt_sleeves": ["control"]}, "config_hash": ""}
)


def ledger(sleeve: str, **positions) -> SleeveLedger:
    led = SleeveLedger(sleeve, D("2000"))
    led.positions = {sym: (D(str(q)), D(str(c))) for sym, (q, c) in positions.items()}
    return led


def ref(coid: str, symbol: str, qty: str, stop: str) -> OrderRef:
    return OrderRef(
        client_order_id=coid,
        symbol=symbol,
        side=Side.SELL,
        qty=D(qty),
        stop_price=D(stop),
        is_protective=True,
        submitted_at=NOW,
    )


def test_naked_lot_arms_at_25_pct_below_cost():
    ledgers = {"momentum": ledger("momentum", IWM=(3, "301.05"))}
    plan = plan_protection(ledgers, (), UNIVERSE, CFG)
    assert len(plan.arms) == 1 and plan.cancels == ()
    stop = plan.arms[0]
    assert stop.symbol == "IWM" and stop.qty == D("3")
    assert stop.stop_price == D("225.78")  # 301.05 * 0.75, floored to tick
    assert is_protective_coid(stop.client_order_id)


def test_covered_lot_is_left_alone():
    ledgers = {"momentum": ledger("momentum", IWM=(3, "301.05"))}
    coid = deterministic_protective_coid("momentum", "IWM", D("3"), D("225.78"))
    plan = plan_protection(
        ledgers, (ref(coid, "IWM", "3", "225.78"),), UNIVERSE, CFG
    )
    assert plan.arms == () and plan.cancels == ()


def test_watchdog_cancel_all_is_healed_next_cycle():
    """The 06:12 incident: coverage stripped, position unchanged => re-arm the
    SAME deterministic id, no special-case code for the cause."""
    ledgers = {"momentum": ledger("momentum", EEM=(15, "65.88"))}
    before = plan_protection(ledgers, (), UNIVERSE, CFG)
    # ... watchdog cancels everything; open orders now empty; next cycle:
    after = plan_protection(ledgers, (), UNIVERSE, CFG)
    assert before.arms == after.arms  # same coid, same price — idempotent


def test_changed_lot_cancels_the_stale_stop_and_arms_fresh():
    """Averaging into a lot moves its cost: the resting stop no longer encodes
    the lot, so it is cancelled and a re-priced one armed."""
    stale_coid = deterministic_protective_coid("momentum", "IWM", D("3"), D("225.78"))
    ledgers = {"momentum": ledger("momentum", IWM=(5, "290.00"))}  # averaged in
    plan = plan_protection(
        ledgers, (ref(stale_coid, "IWM", "3", "225.78"),), UNIVERSE, CFG
    )
    assert plan.cancels == (stale_coid,)
    assert len(plan.arms) == 1
    assert plan.arms[0].qty == D("5")
    assert plan.arms[0].stop_price == D("217.50")  # 290 * 0.75


def test_control_sleeve_is_exempt_and_crypto_is_skipped():
    ledgers = {
        "control": ledger("control", SPY=(1, "769.25")),
        "crypto_momo": ledger("crypto_momo", **{"BTC/USD": (0.0063, "118000")}),
    }
    plan = plan_protection(ledgers, (), UNIVERSE, CFG)
    assert plan.arms == () and plan.cancels == ()


def test_sold_out_lot_cancels_its_orphaned_stop():
    coid = deterministic_protective_coid("momentum", "IWM", D("3"), D("225.78"))
    ledgers = {"momentum": ledger("momentum")}  # flat now
    plan = plan_protection(
        ledgers, (ref(coid, "IWM", "3", "225.78"),), UNIVERSE, CFG
    )
    assert plan.arms == () and plan.cancels == (coid,)


def test_disabled_protection_arms_nothing_but_cancels_leftovers():
    cfg = RiskConfig.model_validate(
        {"protection": {"enabled": False}, "config_hash": ""}
    )
    coid = deterministic_protective_coid("momentum", "IWM", D("3"), D("225.78"))
    ledgers = {"momentum": ledger("momentum", IWM=(3, "301.05"))}
    plan = plan_protection(ledgers, (ref(coid, "IWM", "3", "225.78"),), UNIVERSE, cfg)
    assert plan.arms == ()
    assert plan.cancels == (coid,)


def test_strategy_orders_are_invisible_to_the_reconciler():
    """It must never cancel a non-protective order, whatever its shape."""
    strategy_order = OrderRef(
        client_order_id="nwt-net-IWM-abc123",
        symbol="IWM",
        side=Side.SELL,
        qty=D("3"),
        limit_price=D("299"),
        submitted_at=NOW,
    )
    ledgers = {"momentum": ledger("momentum")}
    plan = plan_protection(ledgers, (strategy_order,), UNIVERSE, CFG)
    assert plan.cancels == ()
