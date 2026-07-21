"""Alert outbox: durable, at-least-once operator notification.

Every alert is INSERTed before any delivery is attempted, so a crashed or
failing sender can never lose the record. An alert is marked delivered only
when ALL registered senders succeed; otherwise it stays pending and
deliver_pending() re-sends through every sender (at-least-once — duplicate
sends are acceptable, silent loss is not). Rows are append-style: only
delivered_at/acked_at are ever updated.

v1 senders are stderr + a JSONL file; push senders arrive in Phase 5 through
the same register_sender seam.
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

Severity = Literal["INFO", "WARN", "CRITICAL", "EMERGENCY"]

SEVERITY_RANK: dict[str, int] = {"INFO": 0, "WARN": 1, "CRITICAL": 2, "EMERGENCY": 3}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    acked_at TEXT
);
"""

_SELECT = (
    "SELECT alert_id, severity, category, message, payload, created_at,"
    " delivered_at, acked_at FROM alerts"
)


class Alert(BaseModel, frozen=True):
    alert_id: int
    severity: Severity
    category: str
    message: str
    payload: dict
    created_at: datetime
    delivered_at: datetime | None
    acked_at: datetime | None


Sender = Callable[[Alert], bool]


def _from_row(row: tuple) -> Alert:
    return Alert(
        alert_id=row[0],
        severity=row[1],
        category=row[2],
        message=row[3],
        payload=json.loads(row[4]),
        created_at=datetime.fromisoformat(row[5]),
        delivered_at=datetime.fromisoformat(row[6]) if row[6] else None,
        acked_at=datetime.fromisoformat(row[7]) if row[7] else None,
    )


class AlertOutbox:
    def __init__(self, db_path: Path | str, now_fn: Callable[[], datetime]) -> None:
        self._now = now_fn
        self._senders: list[Sender] = []
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def register_sender(self, fn: Sender) -> None:
        self._senders.append(fn)

    def raise_alert(
        self, severity: Severity, category: str, message: str, payload: dict
    ) -> Alert:
        now = self._now()
        # JSON round-trip up front so the stored row and the returned Alert agree
        # (Decimal and datetime payload values become strings).
        clean = json.loads(json.dumps(payload, sort_keys=True, default=str))
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO alerts (severity, category, message, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (severity, category, message, json.dumps(clean, sort_keys=True), now.isoformat()),
            )
        alert = Alert(
            alert_id=cursor.lastrowid,
            severity=severity,
            category=category,
            message=message,
            payload=clean,
            created_at=now,
            delivered_at=None,
            acked_at=None,
        )
        return self._attempt(alert)

    def deliver_pending(self) -> int:
        rows = self._conn.execute(
            _SELECT + " WHERE delivered_at IS NULL ORDER BY alert_id"
        ).fetchall()
        delivered = 0
        for row in rows:
            if self._attempt(_from_row(row)).delivered_at is not None:
                delivered += 1
        return delivered

    def ack(self, alert_id: int) -> None:
        with self._conn:
            row = self._conn.execute(
                "SELECT acked_at FROM alerts WHERE alert_id = ?", (alert_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown alert_id: {alert_id}")
            if row[0] is None:
                self._conn.execute(
                    "UPDATE alerts SET acked_at = ? WHERE alert_id = ?",
                    (self._now().isoformat(), alert_id),
                )

    def unacked(self, min_severity: Severity = "INFO") -> list[Alert]:
        floor = SEVERITY_RANK[min_severity]
        rows = self._conn.execute(
            _SELECT + " WHERE acked_at IS NULL ORDER BY alert_id"
        ).fetchall()
        return [a for a in map(_from_row, rows) if SEVERITY_RANK[a.severity] >= floor]

    def _attempt(self, alert: Alert) -> Alert:
        ok = True
        for sender in self._senders:
            try:
                ok = bool(sender(alert)) and ok
            except Exception:
                ok = False  # a broken sender must never take the outbox down
        if not ok:
            return alert
        delivered = self._now()
        with self._conn:
            self._conn.execute(
                "UPDATE alerts SET delivered_at = ? WHERE alert_id = ?",
                (delivered.isoformat(), alert.alert_id),
            )
        return alert.model_copy(update={"delivered_at": delivered})


def stderr_sender(alert: Alert) -> bool:
    print(f"[{alert.severity}] {alert.category}: {alert.message}", file=sys.stderr)
    return True


def jsonl_sender(path: Path | str) -> Sender:
    """Append-only JSONL file sender: one JSON object per line."""
    target = Path(path)

    def send(alert: Alert) -> bool:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(alert.model_dump_json() + "\n")
        return True

    return send
