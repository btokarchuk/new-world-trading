"""Circuit-breaker monitors: consumers of runtime-fed BreakerEvents.

Every rule is a hard trip into the state machine — never a warning. The
high-water mark, loss streak, rejection log, and symbol cooldowns are all
persisted so a restart cannot forget an armed breaker's inputs. Window math
uses event timestamps (data-derived), never the wall clock.
"""

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from nwt_contracts import SAFETY_RANK, TradingState

from ..config import BreakerLimits
from ..context import SymbolCooldown
from ..reasons import ReasonCode
from ..state import TradingStateMachine

_SCHEMA = """
CREATE TABLE IF NOT EXISTS breaker_hwm (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    hwm TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS breaker_round_trips (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    pnl TEXT NOT NULL,
    stop_out INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS breaker_rejections (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS breaker_cooldowns (
    symbol TEXT PRIMARY KEY,
    until TEXT NOT NULL
);
"""


class BreakerEvent(BaseModel, frozen=True):
    kind: str
    ts: datetime
    payload: dict = {}


class CircuitBreakers:
    def __init__(
        self,
        cfg: BreakerLimits,
        state: TradingStateMachine,
        now_fn: Callable[[], datetime],
        hwm_path: Path | str,
    ) -> None:
        self._cfg = cfg
        self._state = state
        self._now = now_fn
        self._conn = sqlite3.connect(str(hwm_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def observe(self, event: BreakerEvent) -> None:
        if event.kind == "equity":
            self._on_equity(event)
        elif event.kind == "round_trip":
            self._on_round_trip(event)
        elif event.kind == "rejection":
            self._on_rejection(event)
        else:
            raise ValueError(f"unknown breaker event kind: {event.kind!r}")

    def cooldowns(self) -> list[SymbolCooldown]:
        now = self._now()
        with self._conn:
            rows = self._conn.execute(
                "SELECT symbol, until FROM breaker_cooldowns ORDER BY symbol"
            ).fetchall()
            parsed = [(symbol, datetime.fromisoformat(until)) for symbol, until in rows]
            expired = [(symbol,) for symbol, until in parsed if until <= now]
            self._conn.executemany("DELETE FROM breaker_cooldowns WHERE symbol = ?", expired)
        return [
            SymbolCooldown(symbol=symbol, until=until) for symbol, until in parsed if until > now
        ]

    def tick(self) -> None:
        self._state.expire_cool_off("consecutive_losses", self._cfg.cool_off_h)

    def _on_equity(self, event: BreakerEvent) -> None:
        equity = Decimal(str(event.payload["equity"]))
        day_open = Decimal(str(event.payload["day_open_equity"]))
        loss = day_open - equity
        if loss >= self._cfg.daily_loss_usd:
            self._trip(
                "daily_loss",
                TradingState.HALTED,
                ReasonCode.DAILY_LOSS,
                f"daily loss {loss} >= {self._cfg.daily_loss_usd}",
            )
        with self._conn:
            row = self._conn.execute("SELECT hwm FROM breaker_hwm WHERE id = 1").fetchone()
            hwm = Decimal(row[0]) if row is not None else equity
            if equity > hwm:
                hwm = equity
            self._conn.execute(
                "INSERT OR REPLACE INTO breaker_hwm (id, hwm) VALUES (1, ?)", (str(hwm),)
            )
        if hwm <= 0:
            return
        drawdown_pct = (hwm - equity) / hwm * 100
        if drawdown_pct >= self._cfg.drawdown_halt_pct:
            self._trip(
                "drawdown_halt",
                TradingState.HALTED,
                ReasonCode.DRAWDOWN_HALT,
                f"drawdown {drawdown_pct}% >= {self._cfg.drawdown_halt_pct}% from HWM {hwm}",
            )
        elif drawdown_pct >= self._cfg.drawdown_warn_pct:
            self._trip(
                "drawdown_warn",
                TradingState.REDUCING,
                ReasonCode.DRAWDOWN_WARN,
                f"drawdown {drawdown_pct}% >= {self._cfg.drawdown_warn_pct}% from HWM {hwm}",
            )

    def _on_round_trip(self, event: BreakerEvent) -> None:
        symbol = str(event.payload["symbol"])
        pnl = Decimal(str(event.payload["pnl"]))
        stop_out = bool(event.payload.get("stop_out", False))
        hours = self._cfg.cooldown_after_stop_h if stop_out else self._cfg.cooldown_after_exit_h
        until = event.ts + timedelta(hours=hours)
        cutoff = event.ts - timedelta(hours=self._cfg.consecutive_window_h)
        with self._conn:
            self._conn.execute(
                "INSERT INTO breaker_round_trips (ts, symbol, pnl, stop_out) VALUES (?, ?, ?, ?)",
                (event.ts.isoformat(), symbol, str(pnl), int(stop_out)),
            )
            row = self._conn.execute(
                "SELECT until FROM breaker_cooldowns WHERE symbol = ?", (symbol,)
            ).fetchone()
            # A later short cooldown never shortens an earlier long one (stop-out lock).
            if row is None or datetime.fromisoformat(row[0]) < until:
                self._conn.execute(
                    "INSERT OR REPLACE INTO breaker_cooldowns (symbol, until) VALUES (?, ?)",
                    (symbol, until.isoformat()),
                )
            rows = self._conn.execute(
                "SELECT seq, ts, pnl FROM breaker_round_trips ORDER BY seq"
            ).fetchall()
            parsed = [(seq, datetime.fromisoformat(ts), Decimal(p)) for seq, ts, p in rows]
            stale = [(seq,) for seq, ts, _ in parsed if ts < cutoff]
            self._conn.executemany("DELETE FROM breaker_round_trips WHERE seq = ?", stale)
        recent = [p for _, ts, p in parsed if ts >= cutoff]
        streak = 0
        for trip_pnl in reversed(recent):
            if trip_pnl >= 0:
                break
            streak += 1
        if streak >= self._cfg.consecutive_losses:
            self._trip(
                "consecutive_losses",
                TradingState.REDUCING,
                ReasonCode.CONSECUTIVE_LOSSES,
                f"{streak} consecutive losing round trips"
                f" within {self._cfg.consecutive_window_h}h",
            )

    def _on_rejection(self, event: BreakerEvent) -> None:
        cutoff = event.ts - timedelta(minutes=self._cfg.rejection_window_min)
        with self._conn:
            self._conn.execute(
                "INSERT INTO breaker_rejections (ts) VALUES (?)", (event.ts.isoformat(),)
            )
            rows = self._conn.execute("SELECT seq, ts FROM breaker_rejections").fetchall()
            parsed = [(seq, datetime.fromisoformat(ts)) for seq, ts in rows]
            stale = [(seq,) for seq, ts in parsed if ts < cutoff]
            self._conn.executemany("DELETE FROM breaker_rejections WHERE seq = ?", stale)
        count = sum(1 for _, ts in parsed if ts >= cutoff)
        if count >= self._cfg.rejection_count:
            self._trip(
                "rejection_storm",
                TradingState.HALTED,
                ReasonCode.REJECTION_STORM,
                f"{count} broker rejections within {self._cfg.rejection_window_min}min",
            )

    def _trip(self, breaker: str, to: TradingState, reason: ReasonCode, detail: str) -> None:
        # A REDUCING-target rule firing while already HALTED must not attempt an
        # unsafe transition; the machine is already at least as safe as the target.
        if SAFETY_RANK[to] < SAFETY_RANK[self._state.state()]:
            return
        self._state.trip(breaker, to, reason, detail)
