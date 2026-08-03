"""The engine<->watchdog contract: heartbeats out, commands in.

Heartbeat semantics — a dead-man's PROMISE, not a pulse. The engine writes
`next_due` every beat: "I will be back by this time." That makes overnight
sleeps and 30-second polls the same shape to a supervisor, so the watchdog
never needs to know the trading calendar to spot a wedged engine.

Beats are written from inside the decision loop after real work completes,
never from a side thread — a thread that keeps beating while the loop is stuck
is worse than no heartbeat at all, because it manufactures false confidence.

Commands flow the other way: the watchdog can demand a HALT, and the engine
honours it at the top of its next iteration. The watchdog's real power is that
it cancels orders at the broker directly — the command row is how it tells the
engine *why* it just lost its orders.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

_HEARTBEAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeats (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    seq      INTEGER NOT NULL,
    ts       TEXT NOT NULL,
    next_due TEXT NOT NULL,
    phase    TEXT NOT NULL,
    detail   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control_commands (
    command_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    command  TEXT NOT NULL,
    issuer   TEXT NOT NULL,
    reason   TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);
"""

Phase = Literal["idle", "waiting_session", "ingest", "cycle", "poll", "shutdown"]


class Heartbeat(BaseModel, frozen=True):
    seq: int
    ts: datetime
    next_due: datetime
    phase: str
    detail: str

    def overdue_by(self, now: datetime) -> timedelta:
        """Positive when the engine has broken its promise."""
        return now - self.next_due


class ControlCommand(BaseModel, frozen=True):
    command_id: int
    ts: datetime
    command: str
    issuer: str
    reason: str


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_HEARTBEAT_SCHEMA)


class SupervisionStore:
    """Engine-side writer. The watchdog reads the same tables read-only."""

    def __init__(self, db_path: Path | str) -> None:
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        init_schema(self.conn)

    def beat(self, now: datetime, next_due: datetime, phase: Phase, detail: str = "") -> None:
        with self.conn:
            row = self.conn.execute("SELECT seq FROM heartbeats WHERE id = 1").fetchone()
            seq = (row[0] + 1) if row else 1
            self.conn.execute(
                "INSERT OR REPLACE INTO heartbeats (id, seq, ts, next_due, phase, detail)"
                " VALUES (1, ?, ?, ?, ?, ?)",
                (seq, now.isoformat(), next_due.isoformat(), phase, detail),
            )

    def last_beat(self) -> Heartbeat | None:
        row = self.conn.execute(
            "SELECT seq, ts, next_due, phase, detail FROM heartbeats WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return Heartbeat(
            seq=row[0],
            ts=datetime.fromisoformat(row[1]),
            next_due=datetime.fromisoformat(row[2]),
            phase=row[3],
            detail=row[4],
        )

    def pending_commands(self) -> list[ControlCommand]:
        rows = self.conn.execute(
            "SELECT command_id, ts, command, issuer, reason FROM control_commands"
            " WHERE consumed = 0 ORDER BY command_id"
        ).fetchall()
        return [
            ControlCommand(
                command_id=r[0],
                ts=datetime.fromisoformat(r[1]),
                command=r[2],
                issuer=r[3],
                reason=r[4],
            )
            for r in rows
        ]

    def consume(self, command_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE control_commands SET consumed = 1 WHERE command_id = ?", (command_id,)
            )

    def issue(self, now: datetime, command: str, issuer: str, reason: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO control_commands (ts, command, issuer, reason)"
                " VALUES (?,?,?,?)",
                (now.isoformat(), command, issuer, reason),
            )
        return int(cur.lastrowid)
