"""Read-only FastAPI over the risk db.

No auth yet: this binds to localhost for one operator's eyes. The phone kill
switch is Phase 6 and needs its own authenticated design — it is deliberately
NOT stubbed here, because a mutation endpoint without that design is a loaded
gun on the kitchen table. Every route is a GET; the db is opened per-request
with sqlite's mode=ro and closed before the response leaves; the factory does
no I/O, so importing or constructing the app never blocks.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .observe import (
    AlertView,
    EquityPoint,
    FillView,
    LatchView,
    ObserverConfig,
    RiskDbUnavailable,
    SleeveView,
    SystemStateView,
    fills_since,
    fold_ledgers,
    gather,
    last_stored_closes,
    open_risk_db,
    read_equity,
    read_heartbeat,
    read_ledger_rows,
    read_pending_commands,
    read_system_state,
    read_unacked_alerts,
    read_unacked_latches,
    sleeve_views,
)
from .report import render_markdown


class HealthOut(BaseModel):
    status: Literal["ok", "overdue", "no_heartbeat", "no_database"]
    now: datetime
    last_beat: datetime | None = None
    promised_back_by: datetime | None = None
    overdue_s: float | None = None
    phase: str | None = None
    detail: str | None = None


class StatusOut(BaseModel):
    state: SystemStateView | None
    latches: tuple[LatchView, ...]
    alerts: tuple[AlertView, ...]
    pending_commands: int


class PositionsOut(BaseModel):
    sleeves: tuple[SleeveView, ...]
    gaps: tuple[str, ...]  # anti-confabulation: unpriceable positions, verbatim


def create_app(
    db_path: Path | str = Path("data/risk.db"),
    paper_config: Path | str = Path("config/paper.yaml"),
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    app = FastAPI(
        title="nwt-backend",
        description="Read-only observer over the engine's risk db. No mutation endpoints.",
        version="0.1.0",
    )

    def _conn():
        try:
            return open_risk_db(db_path)
        except RiskDbUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def _cfg() -> ObserverConfig:
        try:
            return ObserverConfig.load(paper_config)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/health", response_model=HealthOut)
    def health() -> HealthOut:
        # Always 200 with a structured status: a monitor polling this wants an
        # answer about the engine, not an error about the observer.
        now = now_fn()
        try:
            conn = open_risk_db(db_path)
        except RiskDbUnavailable as exc:
            return HealthOut(status="no_database", now=now, detail=str(exc))
        try:
            beat = read_heartbeat(conn, now)
        finally:
            conn.close()
        if beat is None:
            return HealthOut(status="no_heartbeat", now=now)
        return HealthOut(
            status="overdue" if beat.overdue_s > 0 else "ok",
            now=now,
            last_beat=beat.ts,
            promised_back_by=beat.next_due,
            overdue_s=beat.overdue_s,
            phase=beat.phase,
            detail=beat.detail,
        )

    @app.get("/status", response_model=StatusOut)
    def status() -> StatusOut:
        now = now_fn()
        conn = _conn()
        try:
            return StatusOut(
                state=read_system_state(conn),
                latches=read_unacked_latches(conn, now),
                alerts=read_unacked_alerts(conn, now),
                pending_commands=read_pending_commands(conn),
            )
        finally:
            conn.close()

    @app.get("/positions", response_model=PositionsOut)
    def positions() -> PositionsOut:
        cfg = _cfg()
        conn = _conn()
        try:
            ledger_rows = read_ledger_rows(conn)
        finally:
            conn.close()
        ledgers, gaps, broken = fold_ledgers(ledger_rows, cfg)
        held = {
            symbol
            for ledger in ledgers.values()
            for symbol, (qty, _avg) in ledger.positions.items()
            if qty != 0
        }
        marks, mark_gaps = last_stored_closes(cfg, held) if held else ({}, [])
        return PositionsOut(
            sleeves=sleeve_views(ledgers, cfg, marks, broken), gaps=tuple(gaps + mark_gaps)
        )

    @app.get("/fills", response_model=list[FillView])
    def fills(days: int = Query(default=2, ge=1, le=90)) -> list[FillView]:
        conn = _conn()
        try:
            ledger_rows = read_ledger_rows(conn)
        finally:
            conn.close()
        return list(fills_since(ledger_rows, now_fn() - timedelta(days=days)))

    @app.get("/report/today", response_class=PlainTextResponse)
    def report_today() -> PlainTextResponse:
        cfg = _cfg()
        conn = _conn()
        try:
            snapshot = gather(conn, cfg, now_fn(), db_path)
        finally:
            conn.close()
        return PlainTextResponse(render_markdown(snapshot), media_type="text/markdown")

    @app.get("/equity", response_model=list[EquityPoint])
    def equity() -> list[EquityPoint]:
        conn = _conn()
        try:
            return list(read_equity(conn))
        finally:
            conn.close()

    return app
