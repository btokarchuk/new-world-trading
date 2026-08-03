"""Market-aware scheduler: the always-on loop that replaces the container's
idle command.

Every iteration is plan -> promise -> sleep -> work. `plan_next` is a pure
function of the broker clock and config/schedule.yaml; it never consults what
the loop has already done. That purity forces one non-obvious rule on the
loop: it must sleep to the time it planned and then run the action it
planned, never re-derive one after waking. A wake overshoots its target by
milliseconds, and a strictly forward-looking planner asked at 09:00:00.05
answers with the NEXT slot — silently skipping the ingest it just waited for.

Heartbeats follow supervision.py's promise semantics: each beat carries the
time the loop will be back by (next action + heartbeat_grace_s), written from
inside the loop after the work, never from a side thread.

Failure posture: an action that raises is alerted and retried at its next
slot, but three consecutive failures of the same action trip HALTED. A
scheduler that cannot ingest, decide, or collect fills must not keep
pretending it is running the strategy.

Arming stays attended. A HALTED engine skips cycles and polls, so the loop
cannot trade its way out of a breaker latch; the one sanctioned self-heal is
the startup handshake below, which reconciles once at boot so a restarted
daemon resumes when the operator's arming intent survived.
"""

from datetime import datetime, time, timedelta
from pathlib import Path
from time import sleep
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, field_validator, model_validator

from nwt_contracts import TradingState

from .alerts import AlertOutbox, jsonl_sender, stderr_sender
from .config import RiskConfig
from .paper import PaperConfig, PaperCycle, build_paper_cycle
from .reasons import ReasonCode
from .state import TradingStateMachine
from .supervision import Phase, SupervisionStore

Action = Literal["ingest", "cycle", "poll", "wait"]

_PHASE: dict[Action, Phase] = {
    "ingest": "ingest",
    "cycle": "cycle",
    "poll": "poll",
    "wait": "waiting_session",
}

_MAX_FAILURES = 3
_CLOCK_RETRY_S = 60.0
# A "wait" consumes no slot, so the loop wakes a hair BEFORE the slot it is
# waiting for; waking exactly on it would leave the forward-looking planner
# pointing past the ingest that the wait existed to reach.
_WAIT_WAKE_LEAD_S = 1.0
_MINUTES_PER_DAY = 24 * 60


def _hhmm(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute))


class ScheduleConfig(BaseModel, frozen=True):
    """config/schedule.yaml — when the unattended loop does what, in ET."""

    ingest_at_et: str = "09:00"
    cycle_at_et: str = "09:35"
    poll_every_min: int = 30
    eod_poll_at_et: str = "16:05"
    tz: str = "America/New_York"
    heartbeat_grace_s: int = 120

    @field_validator("ingest_at_et", "cycle_at_et", "eod_poll_at_et")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        _hhmm(value)
        return value

    @field_validator("poll_every_min")
    @classmethod
    def _valid_interval(cls, value: int) -> int:
        if not 1 <= value <= 720:
            raise ValueError("poll_every_min must be between 1 and 720")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> "ScheduleConfig":
        try:
            ZoneInfo(self.tz)  # unknown zone => refuse at load, not at 09:35
        except Exception as exc:
            raise ValueError(f"unknown tz {self.tz!r}: {exc}") from exc
        if not _hhmm(self.ingest_at_et) < _hhmm(self.cycle_at_et) < _hhmm(self.eod_poll_at_et):
            raise ValueError(
                "schedule must run ingest_at_et < cycle_at_et < eod_poll_at_et"
            )
        return self

    @classmethod
    def load(cls, path: Path | str) -> "ScheduleConfig":
        import yaml

        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


class Scheduler:
    def __init__(
        self,
        paper_cfg: PaperConfig,
        risk_cfg: RiskConfig,
        schedule_cfg: ScheduleConfig,
        broker,
        db_path: Path | str,
        quotes_loader: Callable[[], dict],
        bars_ingest_fn: Callable[[], object],
        now_fn: Callable[[], datetime],
        supervision: SupervisionStore,
        state_machine: TradingStateMachine,
        sleep_fn: Callable[[float], None] = sleep,
        alerts: AlertOutbox | None = None,
        cycle_factory: Callable[[], PaperCycle] | None = None,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self.paper_cfg = paper_cfg
        self.risk_cfg = risk_cfg
        self.schedule = schedule_cfg
        self.broker = broker
        self.db_path = Path(db_path)
        self.quotes_loader = quotes_loader
        self.bars_ingest_fn = bars_ingest_fn
        self.now_fn = now_fn
        self.supervision = supervision
        self.state_machine = state_machine
        self.sleep_fn = sleep_fn
        self.log_fn = log_fn
        self._tz = ZoneInfo(schedule_cfg.tz)
        self._grace = timedelta(seconds=schedule_cfg.heartbeat_grace_s)
        # Long waits are slept in poll-interval chunks so a watchdog HALT is
        # honoured within one interval even during an overnight sleep.
        self._max_sleep_s = float(schedule_cfg.poll_every_min * 60)
        self._failures: dict[str, int] = {}
        self._halt_noted = False
        self._cycle_obj: PaperCycle | None = None
        self._cycle_factory = cycle_factory or (
            lambda: build_paper_cycle(
                paper_cfg, risk_cfg, broker, self.db_path, quotes_loader, now_fn
            )
        )
        if alerts is None:
            alerts = AlertOutbox(self.db_path, now_fn)
            alerts.register_sender(stderr_sender)
            alerts.register_sender(jsonl_sender(self.db_path.parent / "alerts.jsonl"))
        self.alerts = alerts

    # -- planning ------------------------------------------------------------

    def plan_next(self, now: datetime) -> tuple[Action, datetime]:
        """The next action and the instant it is due, from the broker clock.

        Strictly forward-looking: the returned time is always after `now`, so
        the loop can never execute the same slot twice.
        """
        clock = self.broker.clock()
        et = now.astimezone(self._tz)
        next_open = clock["next_open"].astimezone(self._tz)
        next_close = clock["next_close"].astimezone(self._tz)
        ingest_at = self._slot(et, self.schedule.ingest_at_et)
        cycle_at = self._slot(et, self.schedule.cycle_at_et)
        eod_at = self._slot(et, self.schedule.eod_poll_at_et)

        if clock["is_open"]:
            if et < ingest_at:
                return ("ingest", ingest_at)
            if et < cycle_at:
                return ("cycle", cycle_at)
            boundary = self._next_boundary(et)
            if boundary >= next_close and et < eod_at:
                return ("poll", eod_at)
            return ("poll", boundary)

        if next_open.date() == et.date():
            if et < ingest_at:
                return ("ingest", ingest_at)
            if et < cycle_at:
                return ("cycle", cycle_at)
            # Late or half-day open past the nominal cycle time: decide at the
            # bell instead of skipping the session. A clock reporting closed
            # with the open already behind it is incoherent — fall back to the
            # poll cadence, because a slot in the past would spin the loop.
            if next_open > et:
                return ("cycle", next_open)
            return ("poll", self._next_boundary(et))

        # Closed with the next open on a later date: today's session is over,
        # or today never had one. The clock cannot tell those apart, so the
        # final poll interval before eod_poll_at is claimed for the eod poll
        # on weekdays only. Missing the eod poll strands fills overnight and
        # HALTs the next morning on a reconcile mismatch; the opposite error
        # is one idle broker call on a holiday afternoon.
        eod_window = eod_at - timedelta(minutes=self.schedule.poll_every_min)
        if et.weekday() < 5 and eod_window <= et < eod_at:
            return ("poll", eod_at)
        return ("wait", self._slot(next_open, self.schedule.ingest_at_et))

    def _slot(self, day: datetime, hhmm: str) -> datetime:
        return datetime.combine(day.date(), _hhmm(hhmm), tzinfo=self._tz)

    def _next_boundary(self, et: datetime) -> datetime:
        """The next wall-clock poll boundary, anchored at ET midnight.

        Rebuilt from the date and wall time rather than added as a timedelta so
        a DST transition shifts the boundaries with the clock, not against it.
        """
        step = self.schedule.poll_every_min
        minutes = ((et.hour * 60 + et.minute) // step + 1) * step
        day = et.date() + timedelta(days=minutes // _MINUTES_PER_DAY)
        minutes %= _MINUTES_PER_DAY
        return datetime.combine(day, time(minutes // 60, minutes % 60), tzinfo=self._tz)

    # -- the loop ------------------------------------------------------------

    def run_once(self) -> tuple[Action, datetime]:
        """Honour commands, wait out the plan, do the work, promise the next.

        Blocks until the planned action is due — overnight, if that is what the
        calendar says. Returns the next (action, time), which is what the
        closing heartbeat promises.
        """
        self._honour_commands()
        action, at = self._plan()
        self._wait_for(action, at)
        self._execute(action)
        next_action, next_at = self._plan()
        self._beat(next_at, _PHASE[next_action], f"next {next_action} at {next_at.isoformat()}")
        # The daemon is the only thing driving outbox retries once the operator
        # stops running CLI commands.
        self.alerts.deliver_pending()
        return next_action, next_at

    def run_forever(self) -> None:
        self._startup_handshake()
        try:
            while True:
                self.run_once()
        except KeyboardInterrupt:
            now = self.now_fn()
            # Promise nothing: a stopped engine SHOULD read as overdue.
            self.supervision.beat(now, now, "shutdown", "operator interrupt")
            self._log("shutdown: operator interrupt")
            raise

    def _startup_handshake(self) -> None:
        """Reconcile once at boot.

        Every process start lands HALTED behind the startup latch (state.py),
        and a HALTED loop skips cycles and polls — so without this the daemon
        would idle forever after a restart instead of resuming on the arming
        intent that deliberately survives restarts.
        """
        self._honour_commands()  # a HALT outranks the boot sequence too
        now = self.now_fn()
        self._beat(now + timedelta(seconds=self._max_sleep_s), "idle", "startup reconcile")
        try:
            applied, reconciled = self._collect_and_reconcile()
        except Exception as exc:
            self._on_failure("startup", exc)
            return
        self._failures["startup"] = 0
        self._log(
            f"startup: fills={applied} reconciled={reconciled}"
            f" state={self.state_machine.state().value}"
        )

    def _plan(self) -> tuple[Action, datetime]:
        now = self.now_fn()
        try:
            plan = self.plan_next(now)
        except Exception as exc:
            # No clock means no calendar: retry soon rather than guess.
            self._on_failure("plan", exc)
            return ("wait", now + timedelta(seconds=_CLOCK_RETRY_S))
        self._failures["plan"] = 0
        return plan

    def _wait_for(self, action: Action, at: datetime) -> None:
        target = at - timedelta(seconds=_WAIT_WAKE_LEAD_S) if action == "wait" else at
        while True:
            now = self.now_fn()
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                return
            chunk = min(remaining, self._max_sleep_s)
            self._beat(
                now + timedelta(seconds=chunk),
                _PHASE[action],
                f"{action} due {at.isoformat()}",
            )
            self.sleep_fn(chunk)
            self._honour_commands()

    def _execute(self, action: Action) -> None:
        if action == "wait":
            return
        if action in ("cycle", "poll") and self.state_machine.state() is TradingState.HALTED:
            self._note_halt_skip(action)
            return
        self._halt_noted = False
        try:
            detail = self._run_action(action)
        except Exception as exc:
            self._on_failure(action, exc)
            return
        self._failures[action] = 0
        self._log(f"{action}: {detail}")

    def _run_action(self, action: Action) -> str:
        if action == "ingest":
            result = self.bars_ingest_fn()
            return "bars topped up" if result is None else str(result)
        if action == "cycle":
            report = self._cycle().run_cycle()
            if not report.reconciled:
                self.alerts.raise_alert(
                    "CRITICAL",
                    "scheduler",
                    "cycle aborted: reconcile mismatch — HALTED",
                    {"notes": list(report.notes)},
                )
            for note in report.notes:
                self._log(f"cycle note: {note}")
            return (
                f"state={report.state.value} reconciled={report.reconciled} "
                f"proposals={report.proposals} approved={report.approved} "
                f"submitted={report.submitted} crosses={report.crosses_executed} "
                f"rejections={report.rejected_reasons}"
            )
        applied, reconciled = self._collect_and_reconcile()
        return f"fills={applied} reconciled={reconciled}"

    def _collect_and_reconcile(self) -> tuple[int, bool]:
        cycle = self._cycle()
        ledgers = cycle.store.fold_ledgers(cycle.sleeve_specs)
        applied = cycle.poll_fills(ledgers)
        reconciled = cycle.reconcile_and_arm(ledgers)
        if not reconciled:
            self.alerts.raise_alert(
                "CRITICAL",
                "scheduler",
                "reconcile mismatch — HALTED; books and broker disagree",
                {"fills_applied": applied},
            )
        return applied, reconciled

    def _cycle(self) -> PaperCycle:
        if self._cycle_obj is None:
            self._cycle_obj = self._cycle_factory()
        return self._cycle_obj

    # -- supervision ---------------------------------------------------------

    def _honour_commands(self) -> None:
        """Obey the watchdog before doing anything else.

        Tripped before the row is consumed so a crash in between re-honours the
        command instead of losing it (trip is idempotent per breaker). An
        unrecognised command is honoured as a HALT: a supervisor we cannot
        parse is a supervisor we obey conservatively.
        """
        for command in self.supervision.pending_commands():
            name = command.command.strip().upper()
            self.state_machine.trip(
                "watchdog",
                TradingState.HALTED,
                ReasonCode.KILL_SWITCH,
                f"{name} from {command.issuer}: {command.reason}"[:400],
            )
            self.supervision.consume(command.command_id)
            self.alerts.raise_alert(
                "EMERGENCY" if name == "HALT" else "CRITICAL",
                "watchdog_command",
                f"watchdog {name} honoured — HALTED: {command.reason}",
                {
                    "command_id": command.command_id,
                    "command": command.command,
                    "issuer": command.issuer,
                    "reason": command.reason,
                },
            )
            self._log(f"watchdog {name} honoured — HALTED ({command.reason})")

    def _beat(self, next_due: datetime, phase: Phase, detail: str) -> None:
        self.supervision.beat(self.now_fn(), next_due + self._grace, phase, detail[:200])

    def _note_halt_skip(self, action: Action) -> None:
        self._log(f"{action}: skipped — HALTED")
        if self._halt_noted:
            return
        self._halt_noted = True
        self.alerts.raise_alert(
            "WARN",
            "scheduler",
            "scheduler idling: HALTED — cycles and polls are skipped until an"
            " operator resumes (nwt-risk status / nwt-risk resume)",
            {"action": action},
        )

    def _on_failure(self, action: str, exc: Exception) -> None:
        count = self._failures.get(action, 0) + 1
        self._failures[action] = count
        message = f"{action} failed ({count}/{_MAX_FAILURES}): {type(exc).__name__}: {exc}"
        self.alerts.raise_alert(
            "CRITICAL",
            "scheduler",
            message[:400],
            {"action": action, "error": repr(exc), "consecutive": count},
        )
        self._log(message)
        if count < _MAX_FAILURES:
            return
        self._failures[action] = 0
        self.state_machine.trip(
            "scheduler",
            TradingState.HALTED,
            ReasonCode.KILL_SWITCH,
            f"{_MAX_FAILURES} consecutive {action} failures: {exc}"[:400],
        )
        self.alerts.raise_alert(
            "EMERGENCY",
            "scheduler",
            f"scheduler HALTED: {_MAX_FAILURES} consecutive {action} failures",
            {"action": action, "error": repr(exc)},
        )

    def _log(self, message: str) -> None:
        self.log_fn(f"{self.now_fn().isoformat()} {message}")
