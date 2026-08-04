"""The supervisor loop: read the world, judge it, act on it.

The watchdog's real power is that it cancels at the broker directly — it never
asks the engine to stop, because an engine that could be trusted to stop would
not need supervising. The HALT row it appends to the engine's control_commands
is how the engine learns *why* its orders disappeared.

That row is the ONLY write this package makes to the engine's database, and it
is append-only. Everything else about the risk db is read through a read-only
sqlite URI.

TODO(bracket-orders): the plan's "every open position has a live protective
stop" invariant is missing here, deliberately and without a placeholder. The
engine does not place bracket/stop orders yet (its order body has no
order_class/stop_loss path as of Phase 3), so the check would breach on every
position and the operator would learn to ignore it. It lands the day the engine
starts submitting brackets — a fake check that always fires is worse than a
documented gap.
"""

import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import invariants
from .alerts import WatchdogAlerts
from .config import WatchdogConfig
from .invariants import RATE_WINDOW, Breach

_ISSUER = "watchdog"

# Duplicated from the engine's supervision schema rather than imported: this
# package shares no code with the system it watches. It must stay compatible
# with risk/src/nwt_risk/supervision.py — the engine owns that definition.
_CONTROL_DDL = """
CREATE TABLE IF NOT EXISTS control_commands (
    command_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    command  TEXT NOT NULL,
    issuer   TEXT NOT NULL,
    reason   TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
)
"""

_HEARTBEAT_SELECT = "SELECT seq, ts, next_due, phase, detail FROM heartbeats WHERE id = 1"


def _reason(breaches: list[Breach]) -> str:
    return "watchdog breach: " + "; ".join(
        f"{b.name} observed={b.observed} limit={b.limit}" for b in breaches
    )


class Watchdog:
    def __init__(
        self,
        config: WatchdogConfig,
        broker,
        now_fn: Callable[[], datetime],
        alerts: WatchdogAlerts,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._broker = broker
        self._now = now_fn
        self._alerts = alerts
        self._sleep = sleep_fn
        self._running = False
        # Paging state for repeat suppression. In-memory on purpose: a watchdog
        # restart SHOULD re-page, because a restarted supervisor cannot know
        # whether anyone saw the first alert.
        self._last_page_reason: str | None = None
        self._last_page_at: datetime | None = None
        self._page_backoff_s = float(getattr(config, "page_backoff_s", 900))

    def read_heartbeat(self) -> dict | None:
        """Read-only URI so a watchdog bug cannot corrupt the engine's state.
        Any sqlite failure — missing file, missing table, unreadable WAL —
        collapses to None, which heartbeat_overdue turns into a CRITICAL. Being
        unable to see the heartbeat is indistinguishable from there being none."""
        uri = f"{Path(self._config.risk_db).resolve().as_uri()}?mode=ro"
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True)
            row = conn.execute(_HEARTBEAT_SELECT).fetchone()
        except sqlite3.Error:
            return None
        finally:
            if conn is not None:
                conn.close()
        if row is None:
            return None
        return {"seq": row[0], "ts": row[1], "next_due": row[2], "phase": row[3], "detail": row[4]}

    def check(self) -> list[Breach]:
        now = self._now()
        account = self._broker.account()
        positions = self._broker.positions()
        open_orders = self._broker.open_orders()
        recent_orders = self._broker.orders_since(now - RATE_WINDOW)
        config = self._config
        return [
            *invariants.heartbeat_overdue(self.read_heartbeat(), now, config),
            *invariants.open_order_count(open_orders, config),
            *invariants.gross_exposure(positions, config),
            *invariants.daily_pnl_floor(account, config),
            *invariants.order_creation_rate(recent_orders, now, config),
            *invariants.equity_floor(account, config),
        ]

    def act(self, breaches: list[Breach]) -> None:
        for breach in [b for b in breaches if b.severity == "WARN"]:
            self._alerts.raise_alert("WARN", breach.name, breach.detail, breach.model_dump())
        critical = [b for b in breaches if b.severity == "CRITICAL"]
        if not critical:
            self._alerts.ping_ok()
            return
        # Cancel every poll while the breach stands — an engine wedged badly
        # enough to keep submitting must keep getting cancelled, and choosing
        # which repeat is safe to drop is guesswork.
        #
        # But do NOT re-issue a HALT that is still sitting unconsumed, and do
        # not re-page on every poll: one condition produced 315 identical
        # commands and 315 CRITICALs overnight on 2026-08-03. An alert channel
        # that cries 315 times for one fact trains its reader to ignore it, and
        # the 315th HALT row tells the engine nothing the 1st did not.
        reason = _reason(critical)
        cancel = self._cancel()
        command_id = None
        if not self._halt_outstanding():
            command_id = self._issue_halt(reason)
        if self._should_page(reason):
            self._alerts.raise_alert(
                "CRITICAL",
                "watchdog_breach",
                reason,
                {
                    "breaches": [b.model_dump() for b in critical],
                    "dry_run": self._config.dry_run,
                    "cancel": cancel,
                    "halt_command_id": command_id,
                    "repeat_suppressed_until_s": self._page_backoff_s,
                },
            )
        self._alerts.ping_fail(reason)

    def _halt_outstanding(self) -> bool:
        """True when a HALT we already issued has not been consumed yet."""
        try:
            conn = sqlite3.connect(
                f"file:{self._config.risk_db}?mode=ro", uri=True, timeout=5
            )
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM control_commands"
                    " WHERE consumed = 0 AND issuer = 'watchdog'"
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            return False  # unreadable => assume none, erring toward acting
        return bool(row and row[0])

    def _should_page(self, reason: str) -> bool:
        """First occurrence pages immediately; repeats back off."""
        now = self._now()
        if reason != self._last_page_reason:
            self._last_page_reason = reason
            self._last_page_at = now
            return True
        if self._last_page_at is None:
            self._last_page_at = now
            return True
        elapsed = (now - self._last_page_at).total_seconds()
        if elapsed >= self._page_backoff_s:
            self._last_page_at = now
            return True
        return False

    def run_forever(self) -> None:
        self._running = True
        while self._running:
            try:
                self.act(self.check())
            except Exception as exc:
                self._survive(exc)
            if self._running:
                self._sleep(self._config.poll_interval_s)

    def stop(self) -> None:
        """Clean loop exit for SIGTERM (and for tests)."""
        self._running = False

    def _cancel(self) -> dict:
        if self._config.dry_run:
            # dry_run gates the irreversible broker call and nothing else: the
            # HALT row and the page still land, or a rehearsal would exercise
            # none of the paths that matter.
            return {"dry_run": True, "would_cancel_all": True}
        try:
            return self._broker.cancel_all()
        except Exception as exc:
            # Best-effort by design: if the broker is unreachable, the HALT row
            # and the page are the only levers left, so the caller continues.
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _issue_halt(self, reason: str) -> int | None:
        path = Path(self._config.risk_db)
        conn = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path))
            with conn:
                conn.execute(_CONTROL_DDL)
                cursor = conn.execute(
                    "INSERT INTO control_commands (ts, command, issuer, reason)"
                    " VALUES (?, ?, ?, ?)",
                    (self._now().isoformat(), "HALT", _ISSUER, reason),
                )
            return int(cursor.lastrowid)
        except (sqlite3.Error, OSError) as exc:
            self._alerts.raise_alert(
                "CRITICAL",
                "halt_write_failed",
                f"could not file a HALT in {path}: {type(exc).__name__}: {exc}",
                {"reason": reason},
            )
            return None
        finally:
            if conn is not None:
                conn.close()

    def _survive(self, exc: Exception) -> None:
        # A watchdog that dies on its own exception is the worst failure mode
        # available: everything looks supervised and nothing is. Alert, page,
        # keep polling.
        try:
            self._alerts.raise_alert(
                "CRITICAL",
                "watchdog_error",
                f"watchdog cycle failed: {type(exc).__name__}: {exc}",
                {"traceback": traceback.format_exc()},
            )
            self._alerts.ping_fail(f"watchdog cycle failed: {type(exc).__name__}")
        except Exception:
            print("watchdog alerting failed while handling its own error:", file=sys.stderr)
            traceback.print_exc()
