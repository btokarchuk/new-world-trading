"""Results DB (SQLite, WAL) + append-only event journal.

The journal hash is computed over (seq, ts, type, payload) only — payloads must
never contain wall-clock times or random ids, so identical runs produce
identical hashes. That property is enforced by the determinism test.
"""

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    mode        TEXT NOT NULL,
    config_json TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    journal_hash TEXT
);
CREATE TABLE IF NOT EXISTS events (
    run_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    type    TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS equity_daily (
    run_id   TEXT NOT NULL,
    sleeve_id TEXT NOT NULL,
    ts       TEXT NOT NULL,
    cash     TEXT NOT NULL,
    equity   TEXT NOT NULL,
    PRIMARY KEY (run_id, sleeve_id, ts)
);
CREATE TABLE IF NOT EXISTS orders (
    run_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    sleeve_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side   TEXT NOT NULL,
    qty    TEXT,
    notional TEXT,
    limit_price TEXT,
    state  TEXT NOT NULL,
    ts     TEXT NOT NULL,
    PRIMARY KEY (run_id, client_order_id)
);
CREATE TABLE IF NOT EXISTS fills (
    run_id TEXT NOT NULL,
    fill_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    sleeve_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side   TEXT NOT NULL,
    qty    TEXT NOT NULL,
    price  TEXT NOT NULL,
    fees   TEXT NOT NULL,
    ts     TEXT NOT NULL,
    PRIMARY KEY (run_id, fill_id)
);
CREATE TABLE IF NOT EXISTS metrics (
    run_id TEXT NOT NULL,
    sleeve_id TEXT NOT NULL,
    name  TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (run_id, sleeve_id, name)
);
"""


class ResultsDB:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        self._hasher = hashlib.sha256()

    # -- runs ----------------------------------------------------------------

    def start_run(
        self, run_id: str, experiment_id: str, mode: str, config_json: str, started_at: str
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, experiment_id, mode, config_json,"
            " started_at, status) VALUES (?,?,?,?,?, 'running')",
            (run_id, experiment_id, mode, config_json, started_at),
        )
        for table in ("events", "equity_daily", "orders", "fills", "metrics"):
            self.conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))  # noqa: S608
        self.conn.commit()

    def finish_run(self, run_id: str, ended_at: str, status: str) -> str:
        journal_hash = self._hasher.hexdigest()
        self.conn.execute(
            "UPDATE runs SET ended_at = ?, status = ?, journal_hash = ? WHERE run_id = ?",
            (ended_at, status, journal_hash, run_id),
        )
        self.conn.commit()
        return journal_hash

    # -- journal -------------------------------------------------------------

    def journal(self, run_id: str, seq: int, ts: datetime, type_: str, payload: dict) -> None:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        self.conn.execute(
            "INSERT INTO events (run_id, seq, ts, type, payload) VALUES (?,?,?,?,?)",
            (run_id, seq, ts.isoformat(), type_, blob),
        )
        self._hasher.update(f"{seq}|{ts.isoformat()}|{type_}|{blob}\n".encode())

    # -- typed rows ----------------------------------------------------------

    def record_equity(
        self, run_id: str, sleeve_id: str, ts: datetime, cash: str, equity: str
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity_daily VALUES (?,?,?,?,?)",
            (run_id, sleeve_id, ts.isoformat(), cash, equity),
        )

    def record_order(self, run_id: str, sleeve_id: str, ts: datetime, **kw: str | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO orders (run_id, client_order_id, sleeve_id, symbol,"
            " side, qty, notional, limit_price, state, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                kw["client_order_id"],
                sleeve_id,
                kw["symbol"],
                kw["side"],
                kw.get("qty"),
                kw.get("notional"),
                kw.get("limit_price"),
                kw["state"],
                ts.isoformat(),
            ),
        )

    def record_fill(self, run_id: str, sleeve_id: str, **kw: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO fills (run_id, fill_id, client_order_id, sleeve_id,"
            " symbol, side, qty, price, fees, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                kw["fill_id"],
                kw["client_order_id"],
                sleeve_id,
                kw["symbol"],
                kw["side"],
                kw["qty"],
                kw["price"],
                kw["fees"],
                kw["ts"],
            ),
        )

    def record_metric(self, run_id: str, sleeve_id: str, name: str, value: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO metrics VALUES (?,?,?,?)", (run_id, sleeve_id, name, value)
        )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
