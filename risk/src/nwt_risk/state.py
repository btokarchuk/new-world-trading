"""Persisted, latching trading state machine.

Every trip records a latch and the machine may only drift toward safety on its
own. Moving back toward ACTIVE requires a typed confirmation phrase plus an
explicit acknowledgement of every un-acked latch, except for the two sanctioned
automatic paths: paper-mode post-reconcile resume and the consecutive-losses
cool-off expiry. All state lives in SQLite so a crash or restart rebuilds the
exact latch set; every restart lands HALTED behind a startup latch.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from nwt_contracts import SAFETY_RANK, TradingState

from .reasons import ReasonCode

_SCHEMA = """
CREATE TABLE IF NOT EXISTS system_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL,
    mode TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS breaker_latches (
    latch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    breaker TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL,
    ts TEXT NOT NULL,
    acked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS state_transitions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class Latch(BaseModel, frozen=True):
    latch_id: int
    breaker: str
    reason: ReasonCode
    detail: str
    ts: datetime
    acked: bool


class StateRecord(BaseModel, frozen=True):
    state: TradingState
    mode: str
    latches: tuple[Latch, ...]
    updated_at: datetime


class TransitionResult(BaseModel, frozen=True):
    ok: bool
    state: TradingState
    error: str | None = None


class TradingStateMachine:
    def __init__(self, db_path: Path | str, mode: str, now_fn: Callable[[], datetime]) -> None:
        self._mode = mode
        self._now = now_fn
        # Known only after on_startup(); None forbids the paper auto-resume path.
        self._pre_shutdown_state: TradingState | None = None
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        with self._conn:
            row = self._conn.execute("SELECT state FROM system_state WHERE id = 1").fetchone()
            if row is None:
                # A fresh store is HALTED until an explicit startup/resume flow says otherwise.
                self._conn.execute(
                    "INSERT INTO system_state (id, state, mode, updated_at) VALUES (1, ?, ?, ?)",
                    (TradingState.HALTED.value, mode, self._now().isoformat()),
                )
            else:
                self._conn.execute("UPDATE system_state SET mode = ? WHERE id = 1", (mode,))

    def on_startup(self) -> None:
        now = self._now()
        with self._conn:
            prev = self._read_state()
            self._pre_shutdown_state = prev
            if not self._unacked_exists("startup"):
                self._insert_latch(
                    "startup", ReasonCode.STARTUP, "restart_pending_reconcile", now
                )
            self._set_state(TradingState.HALTED, now)
            self._log(
                now, prev, TradingState.HALTED, "system",
                ReasonCode.STARTUP, "restart_pending_reconcile",
            )

    def current(self) -> StateRecord:
        row = self._conn.execute(
            "SELECT state, updated_at FROM system_state WHERE id = 1"
        ).fetchone()
        return StateRecord(
            state=TradingState(row[0]),
            mode=self._mode,
            latches=self._latches(),
            updated_at=datetime.fromisoformat(row[1]),
        )

    def state(self) -> TradingState:
        return self._read_state()

    def trip(self, breaker: str, to: TradingState, reason: ReasonCode, detail: str) -> None:
        now = self._now()
        with self._conn:
            current = self._read_state()
            if SAFETY_RANK[to] < SAFETY_RANK[current]:
                raise RuntimeError(
                    f"trip may only move toward safety: {current.value} -> {to.value} ({breaker})"
                )
            escalates = SAFETY_RANK[to] > SAFETY_RANK[current]
            if self._unacked_exists(breaker):
                if not escalates:
                    return  # idempotent re-trip: latch already armed, state already there
            else:
                self._insert_latch(breaker, reason, detail, now)
            self._set_state(to, now)
            self._log(now, current, to, breaker, reason, detail)

    def request_transition(
        self, to: TradingState, actor: str, confirmation: str, acked_latch_ids: list[int]
    ) -> TransitionResult:
        now = self._now()
        with self._conn:
            current = self._read_state()
            if SAFETY_RANK[to] >= SAFETY_RANK[current]:
                self._set_state(to, now)
                self._log(now, current, to, actor, ReasonCode.OPERATOR, "requested")
                return TransitionResult(ok=True, state=to)
            # Unsafer direction: typed confirmation + ack of every un-acked latch.
            expected_prefix = f"RESUME {self._mode}"
            if not confirmation or not confirmation.startswith(expected_prefix):
                return TransitionResult(
                    ok=False,
                    state=current,
                    error=f"confirmation must start with {expected_prefix!r}",
                )
            unacked = [latch.latch_id for latch in self._latches() if not latch.acked]
            missing = [i for i in unacked if i not in acked_latch_ids]
            if missing:
                return TransitionResult(
                    ok=False,
                    state=current,
                    error=f"un-acked latches not acknowledged: {missing}",
                )
            self._ack(unacked)
            self._set_state(to, now)
            self._log(now, current, to, actor, ReasonCode.OPERATOR, confirmation)
            return TransitionResult(ok=True, state=to)

    def mark_reconciled(self) -> None:
        # Live mode never self-clears: a human must ack the startup latch.
        if self._mode != "paper":
            return
        now = self._now()
        with self._conn:
            current = self._read_state()
            unacked = [latch for latch in self._latches() if not latch.acked]
            startup = [latch for latch in unacked if latch.breaker == "startup"]
            if not startup:
                return
            self._ack([latch.latch_id for latch in startup])
            clean = self._pre_shutdown_state is TradingState.ACTIVE
            if len(unacked) == 1 and clean:
                self._set_state(TradingState.ACTIVE, now)
                self._log(
                    now, current, TradingState.ACTIVE, "system",
                    ReasonCode.STARTUP, "paper_reconciled_auto_resume",
                )

    def expire_cool_off(self, breaker: str, cool_off_h: int) -> None:
        now = self._now()
        with self._conn:
            current = self._read_state()
            # Only the REDUCING->ACTIVE restore self-clears; HALTED needs a human.
            if current is not TradingState.REDUCING:
                return
            unacked = [latch for latch in self._latches() if not latch.acked]
            if len(unacked) != 1 or unacked[0].breaker != breaker:
                return
            if now - unacked[0].ts <= timedelta(hours=cool_off_h):
                return
            self._ack([unacked[0].latch_id])
            self._set_state(TradingState.ACTIVE, now)
            self._log(
                now, current, TradingState.ACTIVE, "system",
                ReasonCode.COOL_OFF_EXPIRED, f"{breaker} cool-off of {cool_off_h}h elapsed",
            )

    def _read_state(self) -> TradingState:
        row = self._conn.execute("SELECT state FROM system_state WHERE id = 1").fetchone()
        return TradingState(row[0])

    def _latches(self) -> tuple[Latch, ...]:
        rows = self._conn.execute(
            "SELECT latch_id, breaker, reason, detail, ts, acked"
            " FROM breaker_latches ORDER BY latch_id"
        ).fetchall()
        return tuple(
            Latch(
                latch_id=r[0],
                breaker=r[1],
                reason=ReasonCode(r[2]),
                detail=r[3],
                ts=datetime.fromisoformat(r[4]),
                acked=bool(r[5]),
            )
            for r in rows
        )

    def _unacked_exists(self, breaker: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM breaker_latches WHERE breaker = ? AND acked = 0 LIMIT 1", (breaker,)
        ).fetchone()
        return row is not None

    def _insert_latch(self, breaker: str, reason: ReasonCode, detail: str, ts: datetime) -> None:
        self._conn.execute(
            "INSERT INTO breaker_latches (breaker, reason, detail, ts, acked)"
            " VALUES (?, ?, ?, ?, 0)",
            (breaker, reason.value, detail, ts.isoformat()),
        )

    def _ack(self, latch_ids: list[int]) -> None:
        self._conn.executemany(
            "UPDATE breaker_latches SET acked = 1 WHERE latch_id = ?",
            [(i,) for i in latch_ids],
        )

    def _set_state(self, to: TradingState, now: datetime) -> None:
        self._conn.execute(
            "UPDATE system_state SET state = ?, mode = ?, updated_at = ? WHERE id = 1",
            (to.value, self._mode, now.isoformat()),
        )

    def _log(
        self,
        now: datetime,
        from_state: TradingState,
        to_state: TradingState,
        actor: str,
        reason: ReasonCode,
        detail: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO state_transitions (ts, from_state, to_state, actor, reason, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (now.isoformat(), from_state.value, to_state.value, actor, reason.value, detail),
        )
