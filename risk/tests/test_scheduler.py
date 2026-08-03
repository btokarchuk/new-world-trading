"""Scheduler: calendar planning against a fake broker clock, and the loop's
behaviour under watchdog commands and failing actions.

The simulated week is Mon 2026-08-03 .. Fri 2026-08-07 with Thursday removed
as a holiday, plus the following Monday so Friday evening has somewhere to
point. Session hours are the regular 09:30-16:00 ET.
"""

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nwt_contracts import TradingState
from nwt_risk.alerts import AlertOutbox
from nwt_risk.config import RiskConfig
from nwt_risk.paper import CycleReport, PaperConfig
from nwt_risk.reasons import ReasonCode
from nwt_risk.scheduler import ScheduleConfig, Scheduler
from nwt_risk.state import TradingStateMachine
from nwt_risk.supervision import SupervisionStore

ET = ZoneInfo("America/New_York")

_MON, _TUE, _WED, _THU, _FRI = (date(2026, 8, day) for day in (3, 4, 5, 6, 7))
_SAT, _SUN, _NEXT_MON = date(2026, 8, 8), date(2026, 8, 9), date(2026, 8, 10)
_SESSIONS = [_MON, _TUE, _WED, _FRI, _NEXT_MON]  # Thursday is the holiday


def _et(day: date, hhmm: str) -> datetime:
    hour, _, minute = hhmm.partition(":")
    return datetime.combine(day, time(int(hour), int(minute)), tzinfo=ET)


class FakeClockBroker:
    """Only the clock is exercised; the scheduler touches nothing else."""

    def __init__(self, now_fn, sessions: list[date] = _SESSIONS) -> None:
        self.sessions = sorted(sessions)
        self.now_fn = now_fn
        self.fail = False
        self.calls = 0

    def clock(self) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("clock unreachable")
        now = self.now_fn().astimezone(ET)
        for index, day in enumerate(self.sessions):
            if _et(day, "09:30") <= now < _et(day, "16:00"):
                return {
                    "timestamp": now,
                    "is_open": True,
                    "next_open": _et(self.sessions[index + 1], "09:30"),
                    "next_close": _et(day, "16:00"),
                }
        nxt = next(day for day in self.sessions if _et(day, "09:30") > now)
        return {
            "timestamp": now,
            "is_open": False,
            "next_open": _et(nxt, "09:30"),
            "next_close": _et(nxt, "16:00"),
        }


class FakeStore:
    def fold_ledgers(self, specs) -> dict:
        return {}


class FakeCycle:
    def __init__(self, now_fn) -> None:
        self.now_fn = now_fn
        self.store = FakeStore()
        self.sleeve_specs: list = []
        self.fail_cycle = False
        self.fail_poll = False
        self.reconciled = True

    def poll_fills(self, ledgers) -> int:
        if self.fail_poll:
            raise RuntimeError("broker down")
        return 0

    def reconcile_and_arm(self, ledgers) -> bool:
        return self.reconciled

    def run_cycle(self) -> CycleReport:
        if self.fail_cycle:
            raise RuntimeError("cycle exploded")
        return CycleReport(
            ts=self.now_fn(),
            state=TradingState.ACTIVE,
            reconciled=True,
            proposals=1,
            intents=1,
            approved=1,
            submitted=1,
            rejected_reasons={},
            crosses_executed=0,
        )


class Harness:
    """Fake clock + fake sleep: sleeping advances time, and the Nth sleep ends
    the loop, so run_forever runs a bounded number of iterations."""

    def __init__(self, tmp_path: Path, start: datetime, max_sleeps: int = 10_000) -> None:
        self.now = start
        self.max_sleeps = max_sleeps
        self.sleeps: list[float] = []
        self.beats: list[tuple[datetime, datetime, str, str]] = []
        self.log: list[str] = []
        self.ingests = 0

        db = tmp_path / "risk.db"
        self.broker = FakeClockBroker(self.now_fn)
        self.cycle = FakeCycle(self.now_fn)
        self.state = TradingStateMachine(db, "paper", self.now_fn)
        self.supervision = SupervisionStore(db)
        self.alerts = AlertOutbox(db, self.now_fn)
        self._store_beat = self.supervision.beat
        self.supervision.beat = self._record_beat  # type: ignore[method-assign]

        self.scheduler = Scheduler(
            paper_cfg=PaperConfig(universe_files=[], sleeves=[]),
            risk_cfg=RiskConfig(),
            schedule_cfg=ScheduleConfig(),
            broker=self.broker,
            db_path=db,
            quotes_loader=dict,
            bars_ingest_fn=self._ingest,
            now_fn=self.now_fn,
            supervision=self.supervision,
            state_machine=self.state,
            sleep_fn=self.sleep_fn,
            alerts=self.alerts,
            cycle_factory=lambda: self.cycle,
            log_fn=self.log.append,
        )

    def now_fn(self) -> datetime:
        return self.now

    def _ingest(self) -> str:
        self.ingests += 1
        return "bars"

    def _record_beat(self, now, next_due, phase, detail="") -> None:
        self.beats.append((now, next_due, phase, detail))
        self._store_beat(now, next_due, phase, detail)

    def sleep_fn(self, seconds: float) -> None:
        if len(self.sleeps) >= self.max_sleeps:
            raise StopIteration("scheduler loop bounded by the test")
        self.sleeps.append(seconds)
        self.now = self.now + timedelta(seconds=seconds)

    def actions(self) -> list[str]:
        """Actions the loop actually performed, in order, from its own log."""
        done = []
        for line in self.log:
            _ts, _, message = line.partition(" ")
            head = message.split(":")[0]
            if head in ("ingest", "cycle", "poll") and "skipped" not in message:
                done.append(head)
        return done

    def arm(self) -> None:
        result = self.state.request_transition(TradingState.ACTIVE, "test", "RESUME paper", [])
        assert result.ok, result.error


@pytest.fixture()
def harness(tmp_path):
    def build(start: datetime, max_sleeps: int = 10_000) -> Harness:
        return Harness(tmp_path, start, max_sleeps)

    return build


# -- plan_next -------------------------------------------------------------

_WEEK = [
    # Monday, before the ingest slot: top up the tape first.
    (_et(_MON, "07:00"), "ingest", _et(_MON, "09:00")),
    (_et(_MON, "08:59"), "ingest", _et(_MON, "09:00")),
    # Between ingest and cycle (still pre-open): the cycle is next.
    (_et(_MON, "09:05"), "cycle", _et(_MON, "09:35")),
    (_et(_MON, "09:31"), "cycle", _et(_MON, "09:35")),
    # Mid-session: poll on the next 30-minute boundary.
    (_et(_MON, "09:36"), "poll", _et(_MON, "10:00")),
    (_et(_MON, "11:15"), "poll", _et(_MON, "11:30")),
    (_et(_MON, "15:29"), "poll", _et(_MON, "15:30")),
    # A boundary landing at or after the close becomes the eod poll.
    (_et(_MON, "15:31"), "poll", _et(_MON, "16:05")),
    # Restarted inside the post-close gap: the eod poll is still owed.
    (_et(_MON, "16:02"), "poll", _et(_MON, "16:05")),
    # After it: nothing until tomorrow's ingest.
    (_et(_MON, "16:30"), "wait", _et(_TUE, "09:00")),
    (_et(_MON, "23:59"), "wait", _et(_TUE, "09:00")),
    # Wednesday evening skips the Thursday holiday entirely.
    (_et(_WED, "16:30"), "wait", _et(_FRI, "09:00")),
    # Holiday with the market closed all day: wait, never a cycle.
    (_et(_THU, "07:00"), "wait", _et(_FRI, "09:00")),
    (_et(_THU, "10:00"), "wait", _et(_FRI, "09:00")),
    (_et(_THU, "16:30"), "wait", _et(_FRI, "09:00")),
    # Friday evening points at Monday.
    (_et(_FRI, "16:30"), "wait", _et(_NEXT_MON, "09:00")),
    # Weekend: no session closed, so no eod poll is ever claimed.
    (_et(_SAT, "12:00"), "wait", _et(_NEXT_MON, "09:00")),
    (_et(_SAT, "15:50"), "wait", _et(_NEXT_MON, "09:00")),
    (_et(_SUN, "20:00"), "wait", _et(_NEXT_MON, "09:00")),
]


@pytest.mark.parametrize(
    ("now", "action", "at"),
    _WEEK,
    ids=[row[0].strftime("%a-%H%M") + f"-{index}" for index, row in enumerate(_WEEK)],
)
def test_plan_next_across_the_week(harness, now, action, at):
    assert harness(now).scheduler.plan_next(now) == (action, at)


def test_plan_next_reads_the_et_wall_clock_whatever_tz_now_is(harness):
    now = _et(_MON, "11:15")
    assert harness(now).scheduler.plan_next(now.astimezone(UTC)) == (
        "poll",
        _et(_MON, "11:30"),
    )


def test_plan_next_never_points_backwards(harness):
    """The loop's no-spin invariant: the slot returned is always in the future."""
    harn = harness(_et(_MON, "00:00"))
    while harn.now < _et(_NEXT_MON, "00:00"):
        action, at = harn.scheduler.plan_next(harn.now)
        assert at > harn.now, (harn.now, action, at)
        assert action in ("ingest", "cycle", "poll", "wait")
        harn.now += timedelta(minutes=7)


# -- the loop ---------------------------------------------------------------


def test_run_forever_walks_a_session_and_beats_a_promise(harness):
    harn = harness(_et(_MON, "08:00"), max_sleeps=14)
    harn.arm()
    with pytest.raises(StopIteration):
        harn.scheduler.run_forever()

    assert harn.ingests == 1
    assert harn.actions()[:3] == ["ingest", "cycle", "poll"]
    assert harn.actions().count("poll") >= 2
    assert harn.state.state() is TradingState.ACTIVE

    grace = timedelta(seconds=ScheduleConfig().heartbeat_grace_s)
    for ts, next_due, phase, _detail in harn.beats:
        assert next_due > ts + grace          # every promise is a real window
        assert next_due - ts <= timedelta(hours=1) + grace
        assert phase in ("idle", "waiting_session", "ingest", "cycle", "poll")

    last = harn.supervision.last_beat()
    assert last is not None
    assert last.seq == len(harn.beats)
    assert last.overdue_by(last.ts) < timedelta(0)


def test_overnight_wait_keeps_beating_and_still_ingests_next_morning(harness):
    harn = harness(_et(_MON, "16:30"), max_sleeps=60)
    harn.arm()
    with pytest.raises(StopIteration):
        harn.scheduler.run_forever()

    # The wait is slept in poll-interval chunks, each with its own promise, and
    # the loop still lands on Tuesday's ingest rather than sleeping past it.
    assert harn.ingests == 1
    assert harn.actions()[0] == "ingest"
    assert max(harn.sleeps) <= ScheduleConfig().poll_every_min * 60
    assert len(harn.beats) > 20


def test_pending_halt_command_is_consumed_and_trips_the_state_machine(harness):
    harn = harness(_et(_MON, "09:36"), max_sleeps=3)
    harn.arm()
    harn.supervision.issue(harn.now, "HALT", "watchdog", "heartbeat overdue by 400s")

    with pytest.raises(StopIteration):
        harn.scheduler.run_forever()

    assert harn.state.state() is TradingState.HALTED
    assert harn.supervision.pending_commands() == []
    latches = [latch for latch in harn.state.current().latches if latch.breaker == "watchdog"]
    assert len(latches) == 1
    assert "heartbeat overdue by 400s" in latches[0].detail
    assert [alert.category for alert in harn.alerts.unacked("EMERGENCY")] == [
        "watchdog_command"
    ]
    # A halted engine stops trading but never stops beating.
    assert harn.actions() == []
    assert any("skipped — HALTED" in line for line in harn.log)
    assert len(harn.beats) >= 3


def test_unknown_command_is_honoured_as_a_halt(harness):
    harn = harness(_et(_MON, "09:36"), max_sleeps=2)
    harn.arm()
    harn.supervision.issue(harn.now, "SOMETHING_NEW", "watchdog", "future command")

    with pytest.raises(StopIteration):
        harn.scheduler.run_forever()

    assert harn.state.state() is TradingState.HALTED
    assert harn.supervision.pending_commands() == []


def test_raising_action_does_not_kill_the_loop(harness):
    harn = harness(_et(_MON, "09:36"), max_sleeps=4)
    harn.arm()
    calls = {"n": 0}

    def flaky(ledgers) -> int:
        calls["n"] += 1
        if calls["n"] == 2:  # 1 is the startup reconcile; fail the first real poll
            raise RuntimeError("broker down")
        return 0

    harn.cycle.poll_fills = flaky

    with pytest.raises(StopIteration):
        harn.scheduler.run_forever()

    assert any("poll failed (1/3)" in line for line in harn.log)
    assert harn.actions().count("poll") >= 2          # kept polling after the failure
    assert harn.state.state() is TradingState.ACTIVE
    assert len(harn.alerts.unacked("CRITICAL")) == 1


def test_three_consecutive_failures_trip_halted(harness):
    harn = harness(_et(_MON, "09:36"), max_sleeps=6)
    harn.arm()
    harn.cycle.fail_poll = True

    with pytest.raises(StopIteration):
        harn.scheduler.run_forever()

    assert harn.state.state() is TradingState.HALTED
    latches = [latch for latch in harn.state.current().latches if latch.breaker == "scheduler"]
    assert len(latches) == 1
    assert "3 consecutive poll failures" in latches[0].detail
    assert [alert.category for alert in harn.alerts.unacked("EMERGENCY")] == ["scheduler"]
    assert harn.beats[-1][0] > latches[0].ts  # still alive after tripping itself


def test_unreachable_clock_retries_instead_of_dying(harness):
    harn = harness(_et(_MON, "09:36"), max_sleeps=4)
    harn.arm()
    harn.broker.fail = True

    with pytest.raises(StopIteration):
        harn.scheduler.run_forever()

    assert any("plan failed (1/3)" in line for line in harn.log)
    assert harn.state.state() is TradingState.HALTED  # blind is a failure like any other
    assert harn.beats


def test_halted_engine_never_cycles_but_keeps_beating(harness):
    harn = harness(_et(_MON, "08:30"), max_sleeps=6)
    harn.state.trip("test", TradingState.HALTED, ReasonCode.OPERATOR, "operator halt")

    with pytest.raises(StopIteration):
        harn.scheduler.run_forever()

    assert harn.ingests == 1          # data top-ups stay harmless while halted
    assert harn.actions() == ["ingest"]
    assert len(harn.alerts.unacked("WARN")) == 1  # one idling warning, not one per slot
    assert len(harn.beats) >= 4


# -- config -----------------------------------------------------------------


def test_schedule_config_loads_and_refuses_incoherent_files(tmp_path):
    path = tmp_path / "schedule.yaml"
    path.write_text(
        "ingest_at_et: '08:30'\ncycle_at_et: '09:40'\npoll_every_min: 15\n"
        "eod_poll_at_et: '16:10'\ntz: America/New_York\nheartbeat_grace_s: 90\n"
    )
    cfg = ScheduleConfig.load(path)
    assert (cfg.ingest_at_et, cfg.poll_every_min, cfg.heartbeat_grace_s) == ("08:30", 15, 90)

    path.write_text("ingest_at_et: '10:00'\ncycle_at_et: '09:35'\n")
    with pytest.raises(ValueError, match="ingest_at_et < cycle_at_et"):
        ScheduleConfig.load(path)

    path.write_text("tz: Mars/Olympus\n")
    with pytest.raises(ValueError):
        ScheduleConfig.load(path)

    path.write_text("poll_every_min: 0\n")
    with pytest.raises(ValueError, match="poll_every_min"):
        ScheduleConfig.load(path)


def test_repo_schedule_yaml_matches_the_shipped_defaults():
    cfg = ScheduleConfig.load(Path(__file__).parents[2] / "config" / "schedule.yaml")
    assert cfg == ScheduleConfig()
