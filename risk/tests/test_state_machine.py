from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nwt_contracts import SAFETY_RANK, TradingState
from nwt_risk.reasons import ReasonCode
from nwt_risk.state import TradingStateMachine

START = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)

_TRIP_REASON = {
    TradingState.ACTIVE: ReasonCode.OPERATOR,
    TradingState.REDUCING: ReasonCode.CONSECUTIVE_LOSSES,
    TradingState.HALTED: ReasonCode.DAILY_LOSS,
}


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, delta: timedelta) -> None:
        self._t += delta


def _mem_machine(mode: str = "paper") -> tuple[TradingStateMachine, FakeClock]:
    clock = FakeClock()
    machine = TradingStateMachine(":memory:", mode, clock.now)
    machine.on_startup()
    return machine, clock


def _unacked_ids(machine: TradingStateMachine) -> list[int]:
    return [latch.latch_id for latch in machine.current().latches if not latch.acked]


def _resume(machine: TradingStateMachine, mode: str = "paper") -> None:
    res = machine.request_transition(
        TradingState.ACTIVE, "test-op", f"RESUME {mode} 10000", _unacked_ids(machine)
    )
    assert res.ok


# --- (d) startup ---------------------------------------------------------


def test_startup_lands_halted_with_startup_latch():
    machine, _ = _mem_machine()
    assert machine.state() is TradingState.HALTED
    latches = [latch for latch in machine.current().latches if latch.breaker == "startup"]
    assert len(latches) == 1
    assert not latches[0].acked
    assert latches[0].reason is ReasonCode.STARTUP
    assert latches[0].detail == "restart_pending_reconcile"
    # a second startup with the latch still un-acked adds no duplicate
    machine.on_startup()
    assert (
        len([latch for latch in machine.current().latches if latch.breaker == "startup"]) == 1
    )


def test_restart_from_active_lands_halted_with_fresh_latch(tmp_path):
    db = tmp_path / "state.db"
    clock = FakeClock()
    m1 = TradingStateMachine(db, "paper", clock.now)
    m1.on_startup()
    _resume(m1)
    assert m1.state() is TradingState.ACTIVE

    m2 = TradingStateMachine(db, "paper", clock.now)
    m2.on_startup()
    assert m2.state() is TradingState.HALTED
    unacked_startup = [
        latch
        for latch in m2.current().latches
        if latch.breaker == "startup" and not latch.acked
    ]
    assert len(unacked_startup) == 1


@settings(deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["b1", "b2", "b3"]),
            st.sampled_from(list(TradingState)),
        ),
        max_size=10,
    )
)
def test_startup_always_lands_halted(ops):
    clock = FakeClock()
    machine = TradingStateMachine(":memory:", "paper", clock.now)
    machine.on_startup()
    for breaker, to in ops:
        if SAFETY_RANK[to] < SAFETY_RANK[machine.state()]:
            continue
        machine.trip(breaker, to, _TRIP_REASON[to], "prop")
    machine.on_startup()
    assert machine.state() is TradingState.HALTED
    startup_unacked = [
        latch
        for latch in machine.current().latches
        if latch.breaker == "startup" and not latch.acked
    ]
    assert len(startup_unacked) == 1


# --- (b) trip direction --------------------------------------------------


def test_trip_toward_unsafe_raises():
    machine, _ = _mem_machine()
    assert machine.state() is TradingState.HALTED
    for to in (TradingState.ACTIVE, TradingState.REDUCING):
        with pytest.raises(RuntimeError):
            machine.trip("some_breaker", to, ReasonCode.OPERATOR, "x")

    res = machine.request_transition(
        TradingState.REDUCING, "op", "RESUME paper 10000", _unacked_ids(machine)
    )
    assert res.ok and machine.state() is TradingState.REDUCING
    with pytest.raises(RuntimeError):
        machine.trip("some_breaker", TradingState.ACTIVE, ReasonCode.OPERATOR, "x")
    # equal or safer is allowed
    machine.trip("b_equal", TradingState.REDUCING, ReasonCode.DRAWDOWN_WARN, "x")
    machine.trip("b_safer", TradingState.HALTED, ReasonCode.DAILY_LOSS, "x")
    assert machine.state() is TradingState.HALTED


def test_trip_is_idempotent_per_breaker():
    machine, _ = _mem_machine()
    machine.trip("daily_loss", TradingState.HALTED, ReasonCode.DAILY_LOSS, "x")
    machine.trip("daily_loss", TradingState.HALTED, ReasonCode.DAILY_LOSS, "x")
    latches = [latch for latch in machine.current().latches if latch.breaker == "daily_loss"]
    assert len(latches) == 1


# --- (c) persistence -----------------------------------------------------


def test_latches_survive_restart(tmp_path):
    db = tmp_path / "state.db"
    clock = FakeClock()
    m1 = TradingStateMachine(db, "paper", clock.now)
    m1.on_startup()
    clock.advance(timedelta(minutes=3))
    m1.trip("daily_loss", TradingState.HALTED, ReasonCode.DAILY_LOSS, "loss 200")
    before = m1.current()

    m2 = TradingStateMachine(db, "paper", clock.now)
    after = m2.current()
    assert after.state is TradingState.HALTED
    assert after.latches == before.latches
    assert len([latch for latch in after.latches if not latch.acked]) == 2


# --- confirmation / (f) ack rules ----------------------------------------


def test_confirmation_prefix_enforced():
    machine, _ = _mem_machine()
    ids = _unacked_ids(machine)
    for conf in ("", "resume paper 10000", "RESUME live 10000", "HALT paper"):
        res = machine.request_transition(TradingState.ACTIVE, "op", conf, ids)
        assert not res.ok
        assert res.error is not None
        assert machine.state() is TradingState.HALTED
    res = machine.request_transition(TradingState.ACTIVE, "op", "RESUME paper 10000", ids)
    assert res.ok and res.state is TradingState.ACTIVE
    assert machine.state() is TradingState.ACTIVE


def test_toward_safety_requires_no_confirmation():
    machine, _ = _mem_machine()
    _resume(machine)
    res = machine.request_transition(TradingState.HALTED, "op", "", [])
    assert res.ok
    assert machine.state() is TradingState.HALTED


@settings(deadline=None)
@given(st.data())
def test_unsafe_transition_requires_every_unacked_ack(data):
    machine, _ = _mem_machine()
    machine.trip("daily_loss", TradingState.HALTED, ReasonCode.DAILY_LOSS, "x")
    machine.trip("rejection_storm", TradingState.HALTED, ReasonCode.REJECTION_STORM, "x")
    unacked = _unacked_ids(machine)
    assert len(unacked) == 3
    subset = data.draw(st.lists(st.sampled_from(unacked), unique=True))
    res = machine.request_transition(TradingState.ACTIVE, "op", "RESUME paper 10000", subset)
    if set(subset) >= set(unacked):
        assert res.ok and res.state is TradingState.ACTIVE
        assert machine.state() is TradingState.ACTIVE
        assert all(latch.acked for latch in machine.current().latches)
    else:
        assert not res.ok
        assert res.error is not None
        assert machine.state() is TradingState.HALTED
        # a failed request acks nothing
        assert _unacked_ids(machine) == unacked


# --- (e) reconcile flows --------------------------------------------------


def test_paper_auto_resume_when_startup_latch_is_sole_unacked(tmp_path):
    db = tmp_path / "state.db"
    clock = FakeClock()
    m1 = TradingStateMachine(db, "paper", clock.now)
    m1.on_startup()
    _resume(m1)

    m2 = TradingStateMachine(db, "paper", clock.now)
    m2.on_startup()
    m2.mark_reconciled()
    assert m2.state() is TradingState.ACTIVE
    assert all(latch.acked for latch in m2.current().latches)


def test_paper_no_auto_resume_with_other_unacked_latch(tmp_path):
    db = tmp_path / "state.db"
    clock = FakeClock()
    m1 = TradingStateMachine(db, "paper", clock.now)
    m1.on_startup()
    _resume(m1)

    m2 = TradingStateMachine(db, "paper", clock.now)
    m2.on_startup()
    m2.trip("daily_loss", TradingState.HALTED, ReasonCode.DAILY_LOSS, "x")
    m2.mark_reconciled()
    assert m2.state() is TradingState.HALTED
    unacked = [latch.breaker for latch in m2.current().latches if not latch.acked]
    assert unacked == ["daily_loss"]  # startup latch got acked, nothing resumed


def test_paper_no_auto_resume_when_pre_shutdown_not_clean(tmp_path):
    db = tmp_path / "state.db"
    clock = FakeClock()
    m1 = TradingStateMachine(db, "paper", clock.now)
    m1.on_startup()
    res = m1.request_transition(
        TradingState.REDUCING, "op", "RESUME paper 10000", _unacked_ids(m1)
    )
    assert res.ok and m1.state() is TradingState.REDUCING

    m2 = TradingStateMachine(db, "paper", clock.now)
    m2.on_startup()
    m2.mark_reconciled()
    assert m2.state() is TradingState.HALTED
    assert _unacked_ids(m2) == []  # startup latch acked, but no resume


def test_live_mark_reconciled_acks_nothing(tmp_path):
    db = tmp_path / "state.db"
    clock = FakeClock()
    m1 = TradingStateMachine(db, "live", clock.now)
    m1.on_startup()
    _resume(m1, mode="live")

    m2 = TradingStateMachine(db, "live", clock.now)
    m2.on_startup()
    m2.mark_reconciled()
    assert m2.state() is TradingState.HALTED
    unacked = [latch.breaker for latch in m2.current().latches if not latch.acked]
    assert unacked == ["startup"]


# --- (a) global safety invariant -----------------------------------------

_trip_ops = st.tuples(
    st.just("trip"),
    st.sampled_from(["b1", "b2", "b3"]),
    st.sampled_from(list(TradingState)),
)
_resume_ops = st.tuples(
    st.just("resume"),
    st.sampled_from(list(TradingState)),
    st.booleans(),  # good confirmation phrase
    st.booleans(),  # ack every un-acked latch
)


@settings(deadline=None)
@given(st.lists(st.one_of(_trip_ops, _resume_ops), max_size=25))
def test_no_order_permitting_state_with_unacked_halt_latches(ops):
    clock = FakeClock()
    machine = TradingStateMachine(":memory:", "paper", clock.now)
    machine.on_startup()
    # ids of latches created by a HALTED-target trip (startup included)
    halt_latches = {latch.latch_id for latch in machine.current().latches if not latch.acked}
    for op in ops:
        clock.advance(timedelta(minutes=1))
        if op[0] == "trip":
            _, breaker, to = op
            known = {latch.latch_id for latch in machine.current().latches}
            if SAFETY_RANK[to] < SAFETY_RANK[machine.state()]:
                with pytest.raises(RuntimeError):
                    machine.trip(breaker, to, _TRIP_REASON[to], "prop")
            else:
                machine.trip(breaker, to, _TRIP_REASON[to], "prop")
                if to is TradingState.HALTED:
                    halt_latches |= {
                        latch.latch_id
                        for latch in machine.current().latches
                        if latch.latch_id not in known
                    }
        else:
            _, to, good_conf, ack_all = op
            unacked = _unacked_ids(machine)
            conf = "RESUME paper 10000" if good_conf else "wrong phrase"
            machine.request_transition(to, "prop-op", conf, unacked if ack_all else [])

        record = machine.current()
        if record.state is not TradingState.HALTED:
            live_halt = [
                latch.latch_id
                for latch in record.latches
                if not latch.acked and latch.latch_id in halt_latches
            ]
            assert live_halt == []
