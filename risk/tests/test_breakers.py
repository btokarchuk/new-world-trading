from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nwt_contracts import TradingState
from nwt_risk.breakers.monitors import BreakerEvent, CircuitBreakers
from nwt_risk.config import BreakerLimits
from nwt_risk.reasons import ReasonCode
from nwt_risk.state import TradingStateMachine

START = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, delta: timedelta) -> None:
        self._t += delta


def _active_machine(db, clock: FakeClock) -> TradingStateMachine:
    machine = TradingStateMachine(db, "paper", clock.now)
    machine.on_startup()
    ids = [latch.latch_id for latch in machine.current().latches if not latch.acked]
    assert machine.request_transition(TradingState.ACTIVE, "test-op", "RESUME paper 10000", ids).ok
    return machine


def _rig(tmp_path, **cfg_overrides):
    clock = FakeClock()
    db = tmp_path / "risk.db"
    machine = _active_machine(db, clock)
    breakers = CircuitBreakers(BreakerLimits(**cfg_overrides), machine, clock.now, db)
    return machine, breakers, clock, db


def _equity(ts, equity: str, day_open: str | None = None) -> BreakerEvent:
    return BreakerEvent(
        kind="equity", ts=ts, payload={"equity": equity, "day_open_equity": day_open or equity}
    )


def _round_trip(ts, symbol="SPY", pnl="-10", stop_out=False) -> BreakerEvent:
    return BreakerEvent(
        kind="round_trip", ts=ts, payload={"symbol": symbol, "pnl": pnl, "stop_out": stop_out}
    )


def _rejection(ts) -> BreakerEvent:
    return BreakerEvent(kind="rejection", ts=ts, payload={})


def _unacked(machine: TradingStateMachine) -> list[tuple[str, ReasonCode]]:
    return [
        (latch.breaker, latch.reason)
        for latch in machine.current().latches
        if not latch.acked
    ]


# --- daily loss ----------------------------------------------------------


@pytest.mark.parametrize(
    ("loss", "expected_state", "expected_reason"),
    [
        ("199.99", TradingState.ACTIVE, None),
        ("200", TradingState.HALTED, ReasonCode.DAILY_LOSS),
        ("312.50", TradingState.HALTED, ReasonCode.DAILY_LOSS),
    ],
)
def test_daily_loss_boundary(tmp_path, loss, expected_state, expected_reason):
    machine, breakers, clock, _ = _rig(tmp_path)
    day_open = Decimal("10100")
    breakers.observe(_equity(clock.now(), str(day_open - Decimal(loss)), str(day_open)))
    assert machine.state() is expected_state
    if expected_reason is not None:
        assert ("daily_loss", expected_reason) in _unacked(machine)
    else:
        assert _unacked(machine) == []


# --- drawdown ------------------------------------------------------------


@pytest.mark.parametrize(
    ("equity", "expected_state", "expected_latch"),
    [
        ("9400.01", TradingState.ACTIVE, None),  # 5.9999% — one under warn
        ("9400", TradingState.REDUCING, ("drawdown_warn", ReasonCode.DRAWDOWN_WARN)),
        ("9000.01", TradingState.REDUCING, ("drawdown_warn", ReasonCode.DRAWDOWN_WARN)),
        ("9000", TradingState.HALTED, ("drawdown_halt", ReasonCode.DRAWDOWN_HALT)),
    ],
)
def test_drawdown_boundary(tmp_path, equity, expected_state, expected_latch):
    machine, breakers, clock, _ = _rig(tmp_path)
    breakers.observe(_equity(clock.now(), "10000"))  # establish HWM
    clock.advance(timedelta(minutes=1))
    breakers.observe(_equity(clock.now(), equity))  # day_open == equity: no daily-loss trip
    assert machine.state() is expected_state
    if expected_latch is not None:
        assert expected_latch in _unacked(machine)
    else:
        assert _unacked(machine) == []


def test_hwm_persists_across_instances(tmp_path):
    machine, breakers, clock, db = _rig(tmp_path)
    breakers.observe(_equity(clock.now(), "10000"))
    reopened = CircuitBreakers(BreakerLimits(), machine, clock.now, db)
    clock.advance(timedelta(minutes=1))
    reopened.observe(_equity(clock.now(), "9000"))  # 10% off the persisted HWM
    assert machine.state() is TradingState.HALTED
    assert ("drawdown_halt", ReasonCode.DRAWDOWN_HALT) in _unacked(machine)


def test_new_high_resets_drawdown_base(tmp_path):
    machine, breakers, clock, _ = _rig(tmp_path)
    breakers.observe(_equity(clock.now(), "10000"))
    clock.advance(timedelta(minutes=1))
    breakers.observe(_equity(clock.now(), "11000"))  # new HWM
    clock.advance(timedelta(minutes=1))
    breakers.observe(_equity(clock.now(), "10400"))  # 5.45% off 11000 — under warn
    assert machine.state() is TradingState.ACTIVE


# --- consecutive losses --------------------------------------------------


def test_consecutive_losses_boundary(tmp_path):
    machine, breakers, clock, _ = _rig(tmp_path)
    for _ in range(3):
        breakers.observe(_round_trip(clock.now()))
        clock.advance(timedelta(minutes=5))
    assert machine.state() is TradingState.ACTIVE
    breakers.observe(_round_trip(clock.now()))
    assert machine.state() is TradingState.REDUCING
    assert ("consecutive_losses", ReasonCode.CONSECUTIVE_LOSSES) in _unacked(machine)


def test_winning_round_trip_resets_streak(tmp_path):
    machine, breakers, clock, _ = _rig(tmp_path)
    for pnl in ("-1", "-2", "-3", "4.20", "-1", "-2", "-3"):
        breakers.observe(_round_trip(clock.now(), pnl=pnl))
        clock.advance(timedelta(minutes=5))
    assert machine.state() is TradingState.ACTIVE


def test_losses_outside_window_do_not_count(tmp_path):
    machine, breakers, clock, _ = _rig(tmp_path)
    for _ in range(3):
        breakers.observe(_round_trip(clock.now()))
        clock.advance(timedelta(hours=1))
    clock.advance(timedelta(hours=46))  # first loss is now 49h old — outside the 48h window
    breakers.observe(_round_trip(clock.now()))
    assert machine.state() is TradingState.ACTIVE


def test_loss_streak_persists_across_instances(tmp_path):
    machine, breakers, clock, db = _rig(tmp_path)
    for _ in range(3):
        breakers.observe(_round_trip(clock.now()))
        clock.advance(timedelta(minutes=5))
    reopened = CircuitBreakers(BreakerLimits(), machine, clock.now, db)
    reopened.observe(_round_trip(clock.now()))
    assert machine.state() is TradingState.REDUCING


# --- rejection storm -----------------------------------------------------


def test_rejection_storm_boundary(tmp_path):
    machine, breakers, clock, _ = _rig(tmp_path)
    for _ in range(4):
        breakers.observe(_rejection(clock.now()))
        clock.advance(timedelta(minutes=1))
    assert machine.state() is TradingState.ACTIVE
    breakers.observe(_rejection(clock.now()))
    assert machine.state() is TradingState.HALTED
    assert ("rejection_storm", ReasonCode.REJECTION_STORM) in _unacked(machine)


def test_rejections_outside_window_do_not_count(tmp_path):
    machine, breakers, clock, _ = _rig(tmp_path)
    for _ in range(4):
        breakers.observe(_rejection(clock.now()))
        clock.advance(timedelta(minutes=1))
    clock.advance(timedelta(minutes=10))  # earlier four now fall outside the 10min window
    breakers.observe(_rejection(clock.now()))
    assert machine.state() is TradingState.ACTIVE


# --- cooldowns -----------------------------------------------------------


def test_cooldown_durations_exit_vs_stop(tmp_path):
    _, breakers, clock, _ = _rig(tmp_path)
    t0 = clock.now()
    breakers.observe(_round_trip(t0, symbol="AAPL", pnl="5", stop_out=False))
    breakers.observe(_round_trip(t0, symbol="MSFT", pnl="-10", stop_out=True))
    locked = {c.symbol: c.until for c in breakers.cooldowns()}
    assert locked == {"AAPL": t0 + timedelta(hours=4), "MSFT": t0 + timedelta(hours=24)}
    clock.advance(timedelta(hours=5))
    assert [c.symbol for c in breakers.cooldowns()] == ["MSFT"]
    clock.advance(timedelta(hours=20))
    assert breakers.cooldowns() == []


def test_cooldown_keeps_furthest_lock(tmp_path):
    _, breakers, clock, _ = _rig(tmp_path)
    t0 = clock.now()
    breakers.observe(_round_trip(t0, symbol="AAPL", pnl="-10", stop_out=True))
    clock.advance(timedelta(hours=1))
    breakers.observe(_round_trip(clock.now(), symbol="AAPL", pnl="1", stop_out=False))
    locked = {c.symbol: c.until for c in breakers.cooldowns()}
    assert locked["AAPL"] == t0 + timedelta(hours=24)  # 4h exit lock never shortens stop lock


def test_cooldowns_persist_across_instances(tmp_path):
    machine, breakers, clock, db = _rig(tmp_path)
    t0 = clock.now()
    breakers.observe(_round_trip(t0, symbol="AAPL", pnl="-1", stop_out=True))
    reopened = CircuitBreakers(BreakerLimits(), machine, clock.now, db)
    locked = {c.symbol: c.until for c in reopened.cooldowns()}
    assert locked == {"AAPL": t0 + timedelta(hours=24)}


# --- cool-off expiry -----------------------------------------------------


def test_cool_off_restores_active_only_after_window(tmp_path):
    machine, breakers, clock, _ = _rig(tmp_path)
    for _ in range(4):
        breakers.observe(_round_trip(clock.now()))
    assert machine.state() is TradingState.REDUCING
    breakers.tick()
    assert machine.state() is TradingState.REDUCING
    clock.advance(timedelta(hours=24))  # exactly at the limit: not yet older than cool_off_h
    breakers.tick()
    assert machine.state() is TradingState.REDUCING
    clock.advance(timedelta(seconds=1))
    breakers.tick()
    assert machine.state() is TradingState.ACTIVE
    assert _unacked(machine) == []


def test_cool_off_noop_when_not_sole_latch(tmp_path):
    machine, breakers, clock, _ = _rig(tmp_path)
    breakers.observe(_equity(clock.now(), "10000"))
    for _ in range(4):
        breakers.observe(_round_trip(clock.now()))
    assert machine.state() is TradingState.REDUCING
    clock.advance(timedelta(minutes=1))
    breakers.observe(_equity(clock.now(), "9300"))  # 7% drawdown: second un-acked latch
    clock.advance(timedelta(hours=25))
    breakers.tick()
    assert machine.state() is TradingState.REDUCING
    assert ("consecutive_losses", ReasonCode.CONSECUTIVE_LOSSES) in _unacked(machine)
    assert ("drawdown_warn", ReasonCode.DRAWDOWN_WARN) in _unacked(machine)


# --- idempotency / safety of re-trips ------------------------------------


def test_trips_are_idempotent_per_breaker(tmp_path):
    machine, breakers, clock, _ = _rig(tmp_path)
    breakers.observe(_equity(clock.now(), "10000"))
    for _ in range(2):
        clock.advance(timedelta(minutes=1))
        breakers.observe(_equity(clock.now(), "9300"))
    warn = [latch for latch in machine.current().latches if latch.breaker == "drawdown_warn"]
    assert len(warn) == 1
    for _ in range(2):
        clock.advance(timedelta(minutes=1))
        breakers.observe(_equity(clock.now(), "9300", "9600"))  # daily loss 300 on top
    daily = [latch for latch in machine.current().latches if latch.breaker == "daily_loss"]
    assert len(daily) == 1
    assert machine.state() is TradingState.HALTED


def test_reducing_rule_after_halt_does_not_raise(tmp_path):
    machine, breakers, clock, _ = _rig(tmp_path)
    breakers.observe(_equity(clock.now(), "10000"))
    breakers.observe(_equity(clock.now(), "9800", "10000"))  # daily loss 200 => HALT
    assert machine.state() is TradingState.HALTED
    clock.advance(timedelta(minutes=1))
    breakers.observe(_equity(clock.now(), "9300"))  # warn-level drawdown while HALTED: no-op
    assert machine.state() is TradingState.HALTED


def test_unknown_event_kind_rejected(tmp_path):
    _, breakers, clock, _ = _rig(tmp_path)
    with pytest.raises(ValueError):
        breakers.observe(BreakerEvent(kind="mystery", ts=clock.now(), payload={}))
