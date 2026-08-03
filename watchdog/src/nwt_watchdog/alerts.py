"""Watchdog alerting: its own sqlite file, its own stderr, its own HTTP client.

Shares NO delivery path with the engine's outbox on purpose. If the supervisor
wrote its alerts through the machinery it supervises, then a wedged, corrupted,
or disk-full engine would silence the very report of that failure. Separate
file, separate table, separate client — a compromised engine can stop beating,
but it cannot stop the page.

Delivery is best-effort and NEVER raises: a webhook 500 must not stop the
watchdog from cancelling orders. Every attempt lands in `deliveries` with its
outcome, so a channel that has been quietly failing for a week is discoverable
rather than assumed healthy.
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

import httpx

_TIMEOUT_S = 5.0

Severity = Literal["INFO", "WARN", "CRITICAL"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchdog_alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    message  TEXT NOT NULL,
    payload  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    channel  TEXT NOT NULL,
    alert_id INTEGER,
    ok       INTEGER NOT NULL,
    detail   TEXT NOT NULL
);
"""


class WatchdogAlerts:
    def __init__(
        self,
        db_path: Path | str,
        now_fn: Callable[[], datetime],
        webhook_url: str | None = None,
        healthcheck_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._now = now_fn
        self._webhook = webhook_url
        self._healthcheck = healthcheck_url.rstrip("/") if healthcheck_url else None
        self._client = client
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def raise_alert(self, severity: Severity, category: str, message: str, payload: dict) -> int:
        """Record first, deliver second: a sender that dies mid-flight can lose
        the notification but never the evidence."""
        clean = json.loads(json.dumps(payload, sort_keys=True, default=str))
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO watchdog_alerts (ts, severity, category, message, payload)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    self._now().isoformat(),
                    severity,
                    category,
                    message,
                    json.dumps(clean, sort_keys=True),
                ),
            )
        alert_id = int(cursor.lastrowid)
        print(f"[{severity}] watchdog {category}: {message}", file=sys.stderr)
        if self._webhook:
            self._post_webhook(alert_id, severity, category, message, clean)
        return alert_id

    def ping_ok(self) -> None:
        """Healthchecks.io dead-man: silence from the watchdog itself has to
        page someone, or the supervisor becomes another unmonitored process."""
        self._ping(self._healthcheck, "healthcheck_ok", None)

    def ping_fail(self, reason: str) -> None:
        if self._healthcheck is None:
            return
        self._ping(f"{self._healthcheck}/fail", "healthcheck_fail", reason)

    def alerts(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT alert_id, ts, severity, category, message, payload FROM watchdog_alerts"
            " ORDER BY alert_id"
        ).fetchall()
        return [
            {
                "alert_id": r[0],
                "ts": r[1],
                "severity": r[2],
                "category": r[3],
                "message": r[4],
                "payload": json.loads(r[5]),
            }
            for r in rows
        ]

    def deliveries(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT delivery_id, ts, channel, alert_id, ok, detail FROM deliveries"
            " ORDER BY delivery_id"
        ).fetchall()
        return [
            {
                "delivery_id": r[0],
                "ts": r[1],
                "channel": r[2],
                "alert_id": r[3],
                "ok": bool(r[4]),
                "detail": r[5],
            }
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=_TIMEOUT_S)
        return self._client

    def _post_webhook(
        self, alert_id: int, severity: str, category: str, message: str, payload: dict
    ) -> None:
        body = {
            "source": "nwt-watchdog",
            "ts": self._now().isoformat(),
            "severity": severity,
            "category": category,
            "message": message,
            "payload": payload,
        }
        try:
            response = self._http().post(self._webhook, json=body, timeout=_TIMEOUT_S)
            ok = response.status_code < 400
            detail = f"HTTP {response.status_code}"
        except Exception as exc:
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        self._record("webhook", alert_id, ok, detail)

    def _ping(self, url: str | None, channel: str, reason: str | None) -> None:
        if url is None:
            return
        try:
            params = {"reason": reason} if reason else None
            response = self._http().get(url, params=params, timeout=_TIMEOUT_S)
            ok = response.status_code < 400
            detail = f"HTTP {response.status_code}"
        except Exception as exc:
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        self._record(channel, None, ok, detail)

    def _record(self, channel: str, alert_id: int | None, ok: bool, detail: str) -> None:
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO deliveries (ts, channel, alert_id, ok, detail)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (self._now().isoformat(), channel, alert_id, 1 if ok else 0, detail),
                )
        except sqlite3.Error as exc:
            # Last resort: the alert db itself is gone. stderr is the only
            # channel left and it must not take the watchdog down with it.
            print(f"[CRITICAL] watchdog alert-db write failed: {exc}", file=sys.stderr)
        if not ok:
            print(f"[WARN] watchdog {channel} delivery failed: {detail}", file=sys.stderr)
