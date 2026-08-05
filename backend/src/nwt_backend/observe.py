"""Read-only view over the engine's risk db: rows in, typed evidence out.

This package is an OBSERVER. Two laws govern it:

1. Every connection is opened with sqlite's ``mode=ro`` URI, so a bug here is
   physically unable to move the book. There is no write path to misuse.
2. Every derived number must trace to a row that was actually read (or to a
   stored daily bar). What cannot be traced is recorded as a gap — a literal
   "cannot explain ... from evidence" string — never papered over. The gaps
   ride along in the snapshot so the report and the API surface them verbatim.

Schemas are read as data, not imported as code: the table shapes live in
nwt_risk (paper.py, state.py, alerts.py, supervision.py) and the observer
deliberately does not import that package. The one sanctioned code reuse is
nwt_engine.sleeves — folding ledger entries through the real SleeveLedger is
what makes the reported positions the engine's positions, not a reimplementation.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from nwt_contracts import TradingState
from nwt_engine.sleeves import LedgerEntry, LedgerInvariantError, SleeveLedger


class RiskDbUnavailable(RuntimeError):
    """The risk db cannot be opened read-only (missing file, unreadable path)."""


def open_risk_db(path: Path | str) -> sqlite3.Connection:
    # mode=ro is the enforcement, not a convention: sqlite itself refuses
    # writes on this connection, so no code path in this package can touch
    # the engine's book even by accident. Same idiom as the watchdog.
    p = Path(path)
    if not p.exists():
        raise RiskDbUnavailable(f"risk db not found at {p}")
    try:
        return sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError as exc:  # pragma: no cover - exotic fs states
        raise RiskDbUnavailable(f"cannot open {p} read-only: {exc}") from exc


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


# -- config ------------------------------------------------------------------


class SleeveCapital(BaseModel, frozen=True):
    sleeve_id: str
    capital: Decimal


class ObserverConfig(BaseModel, frozen=True):
    """The slice of config/paper.yaml the observer needs: sleeve capitals to
    seed the ledger fold, and where the stored daily bars live for marks.
    Parsed here rather than through nwt_risk.PaperConfig so the observer never
    pulls in the broker import chain that module carries."""

    sleeves: tuple[SleeveCapital, ...]
    data_root: Path = Path("data/parquet")
    data_provider: str = "alpaca"

    @classmethod
    def load(cls, path: Path | str) -> "ObserverConfig":
        import yaml

        raw = yaml.safe_load(Path(path).read_text()) or {}
        return cls(
            sleeves=tuple(
                SleeveCapital(sleeve_id=s["sleeve_id"], capital=Decimal(str(s["capital"])))
                for s in raw.get("sleeves", [])
            ),
            data_root=Path(raw.get("data_root", "data/parquet")),
            data_provider=raw.get("data_provider", "alpaca"),
        )


# -- typed evidence ----------------------------------------------------------


class SystemStateView(BaseModel, frozen=True):
    state: TradingState
    mode: str
    updated_at: datetime
    armed: bool


class LatchView(BaseModel, frozen=True):
    latch_id: int
    breaker: str
    reason: str
    detail: str
    ts: datetime
    acked: bool
    age_s: float


class AlertView(BaseModel, frozen=True):
    alert_id: int
    severity: str
    category: str
    message: str
    created_at: datetime
    delivered_at: datetime | None
    acked_at: datetime | None
    age_s: float


class HeartbeatView(BaseModel, frozen=True):
    seq: int
    ts: datetime
    next_due: datetime
    phase: str
    detail: str
    overdue_s: float  # positive means the engine's promise is broken


class FillView(BaseModel, frozen=True):
    sleeve_id: str
    ts: datetime
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fees: Decimal


class PositionRow(BaseModel, frozen=True):
    symbol: str
    qty: Decimal
    avg_cost: Decimal
    mark: Decimal | None       # last STORED daily close; None = no evidence
    market_value: Decimal | None
    open_pnl: Decimal | None


class SleeveView(BaseModel, frozen=True):
    sleeve_id: str
    capital: Decimal
    cash: Decimal
    positions: tuple[PositionRow, ...]
    equity: Decimal | None     # None whenever any held symbol has no mark
    # False when the ledger fold broke on this sleeve: cash/qty then reflect
    # only the entries before the break, and marks/values/equity are withheld.
    fold_complete: bool = True


class EquityPoint(BaseModel, frozen=True):
    day: str
    equity: Decimal


class ActivityView(BaseModel, frozen=True):
    since: datetime
    audit_counts: dict[str, int]
    # Cycles and polls both audit a reconcile pass and nothing in the audit
    # schema distinguishes them, so the honest number is the combined one.
    # Separately, the engine writes every pass TWICE (once inside
    # ReconcileEngine.reconcile, once in reconcile_and_arm) with the identical
    # inner report, so passes are counted as distinct report timestamps, not
    # audit rows — audit_counts["reconcile"] stays the raw row count.
    reconcile_events: int
    orders_submitted: int
    orders_rejected: int
    rejections_by_reason: dict[str, int]
    fills_applied: int
    # Upper bound on how many ledger fill entries the fill/cross audit rows
    # can explain (a fill row explains up to its net plan's residual legs, a
    # cross row exactly its legs). None = the bound could not be established
    # from evidence, so no partial-coverage claim is made either way.
    fill_explain_capacity: int | None = None


class Snapshot(BaseModel, frozen=True):
    generated_at: datetime
    db_path: str
    empty: bool
    state: SystemStateView | None
    latches: tuple[LatchView, ...]   # un-acked only
    alerts: tuple[AlertView, ...]    # un-acked only
    heartbeat: HeartbeatView | None
    pending_commands: int
    fills_today: tuple[FillView, ...]
    fills_yesterday: tuple[FillView, ...]
    sleeves: tuple[SleeveView, ...]
    equity: tuple[EquityPoint, ...]
    activity: ActivityView
    gaps: tuple[str, ...]            # every entry starts with "Cannot explain"


# -- granular readers (each tolerates the table simply not existing) ---------


def read_system_state(conn: sqlite3.Connection) -> SystemStateView | None:
    if "system_state" not in _tables(conn):
        return None
    row = conn.execute(
        "SELECT state, mode, updated_at FROM system_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    try:
        armed_row = conn.execute("SELECT armed FROM system_state WHERE id = 1").fetchone()
        armed = bool(armed_row[0])
    except sqlite3.OperationalError:
        armed = False  # db predates the armed column
    return SystemStateView(
        state=TradingState(row[0]),
        mode=row[1],
        updated_at=datetime.fromisoformat(row[2]),
        armed=armed,
    )


def read_unacked_latches(conn: sqlite3.Connection, now: datetime) -> tuple[LatchView, ...]:
    if "breaker_latches" not in _tables(conn):
        return ()
    rows = conn.execute(
        "SELECT latch_id, breaker, reason, detail, ts, acked FROM breaker_latches"
        " WHERE acked = 0 ORDER BY latch_id"
    ).fetchall()
    return tuple(
        LatchView(
            latch_id=r[0],
            breaker=r[1],
            reason=r[2],
            detail=r[3],
            ts=datetime.fromisoformat(r[4]),
            acked=bool(r[5]),
            age_s=(now - datetime.fromisoformat(r[4])).total_seconds(),
        )
        for r in rows
    )


def read_unacked_alerts(conn: sqlite3.Connection, now: datetime) -> tuple[AlertView, ...]:
    if "alerts" not in _tables(conn):
        return ()
    rows = conn.execute(
        "SELECT alert_id, severity, category, message, created_at, delivered_at, acked_at"
        " FROM alerts WHERE acked_at IS NULL ORDER BY alert_id"
    ).fetchall()
    return tuple(
        AlertView(
            alert_id=r[0],
            severity=r[1],
            category=r[2],
            message=r[3],
            created_at=datetime.fromisoformat(r[4]),
            delivered_at=datetime.fromisoformat(r[5]) if r[5] else None,
            acked_at=datetime.fromisoformat(r[6]) if r[6] else None,
            age_s=(now - datetime.fromisoformat(r[4])).total_seconds(),
        )
        for r in rows
    )


def read_heartbeat(conn: sqlite3.Connection, now: datetime) -> HeartbeatView | None:
    if "heartbeats" not in _tables(conn):
        return None
    row = conn.execute(
        "SELECT seq, ts, next_due, phase, detail FROM heartbeats WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    next_due = datetime.fromisoformat(row[2])
    return HeartbeatView(
        seq=row[0],
        ts=datetime.fromisoformat(row[1]),
        next_due=next_due,
        phase=row[3],
        detail=row[4],
        overdue_s=(now - next_due).total_seconds(),
    )


def read_pending_commands(conn: sqlite3.Connection) -> int:
    if "control_commands" not in _tables(conn):
        return 0
    row = conn.execute("SELECT COUNT(*) FROM control_commands WHERE consumed = 0").fetchone()
    return int(row[0])


def read_equity(conn: sqlite3.Connection) -> tuple[EquityPoint, ...]:
    if "day_open_equity" not in _tables(conn):
        return ()
    rows = conn.execute("SELECT day, equity FROM day_open_equity ORDER BY day").fetchall()
    return tuple(EquityPoint(day=r[0], equity=Decimal(r[1])) for r in rows)


def read_ledger_rows(conn: sqlite3.Connection) -> list[tuple[str, LedgerEntry]]:
    if "ledger_entries" not in _tables(conn):
        return []
    rows = conn.execute(
        "SELECT sleeve_id, entry_json FROM ledger_entries ORDER BY entry_id"
    ).fetchall()
    return [(sleeve_id, LedgerEntry.model_validate_json(js)) for sleeve_id, js in rows]


def fills_since(
    ledger_rows: list[tuple[str, LedgerEntry]], since: datetime
) -> tuple[FillView, ...]:
    out = []
    for sleeve_id, entry in ledger_rows:
        if entry.kind != "fill" or entry.ts < since:
            continue
        out.append(
            FillView(
                sleeve_id=sleeve_id,
                ts=entry.ts,
                symbol=entry.symbol or "?",
                side=entry.side.value if entry.side else "?",
                qty=entry.qty,
                price=entry.price,
                fees=entry.fees,
            )
        )
    return tuple(sorted(out, key=lambda f: (f.ts, f.sleeve_id)))


def fold_ledgers(
    ledger_rows: list[tuple[str, LedgerEntry]], cfg: ObserverConfig
) -> tuple[dict[str, SleeveLedger], list[str], frozenset[str]]:
    """The same fold the engine runs, seeded from the same config capitals.

    A sleeve whose journal produces an entry the fold cannot apply — an
    invariant violation, or a malformed row (a fill with no side, a zero-qty
    buy) — is BROKEN from that entry on: the fold stops there, the remainder
    is counted as a gap, and the sleeve id is returned so callers suppress
    every derived number instead of presenting a half-folded book as fact.
    """
    gaps: list[str] = []
    ledgers = {s.sleeve_id: SleeveLedger(s.sleeve_id, s.capital) for s in cfg.sleeves}
    unknown: dict[str, int] = {}
    broken: dict[str, int] = {}  # sleeve_id -> entries skipped after the break
    for sleeve_id, entry in ledger_rows:
        ledger = ledgers.get(sleeve_id)
        if ledger is None:
            unknown[sleeve_id] = unknown.get(sleeve_id, 0) + 1
            continue
        if sleeve_id in broken:
            broken[sleeve_id] += 1
            continue
        try:
            ledger.apply(entry)
        except LedgerInvariantError as exc:
            broken[sleeve_id] = 0
            gaps.append(
                f"Cannot explain ledger for sleeve '{sleeve_id}' from evidence:"
                f" fold violated an invariant ({exc})"
            )
        except (AssertionError, ArithmeticError) as exc:
            # Malformed evidence must become a gap, never a crash: apply()
            # asserts fills carry a symbol and side, and a zero-qty buy
            # divides by zero computing the new average cost.
            broken[sleeve_id] = 0
            gaps.append(
                f"Cannot explain ledger for sleeve '{sleeve_id}' from evidence:"
                f" entry could not be applied ({exc!r})"
            )
    for sleeve_id, skipped in sorted(broken.items()):
        if skipped:
            gaps.append(
                f"Cannot explain {skipped} subsequent ledger"
                f" entr{'y' if skipped == 1 else 'ies'} for sleeve '{sleeve_id}'"
                " from evidence: the fold stopped at the first entry it could"
                " not apply"
            )
    for sleeve_id, count in sorted(unknown.items()):
        gaps.append(
            f"Cannot explain {count} ledger entr{'y' if count == 1 else 'ies'} for"
            f" sleeve '{sleeve_id}' from evidence: no such sleeve in the paper config"
        )
    return ledgers, gaps, frozenset(broken)


def last_stored_closes(
    cfg: ObserverConfig, symbols: set[str]
) -> tuple[dict[str, Decimal], list[str]]:
    """Marks come from the last STORED daily bar — never from a network call.
    A symbol with no stored bars gets no mark and an explicit gap."""
    # Imported lazily: ParquetStore drags pandas in, and only the endpoints
    # that price positions should pay for that.
    from nwt_engine.data import ParquetStore
    from nwt_engine.domain import Timeframe

    store = ParquetStore(cfg.data_root)
    marks: dict[str, Decimal] = {}
    gaps: list[str] = []
    for symbol in sorted(symbols):
        try:
            bars = store.read_bars(cfg.data_provider, Timeframe.D1, symbol)
        except FileNotFoundError:
            bars = []
        if bars:
            marks[symbol] = max(bars, key=lambda b: b.ts_close).close
        else:
            gaps.append(
                f"Cannot explain open P&L for {symbol} from evidence:"
                f" no stored daily close under {cfg.data_root}"
            )
    return marks, gaps


def sleeve_views(
    ledgers: dict[str, SleeveLedger],
    cfg: ObserverConfig,
    marks: dict[str, Decimal],
    broken: frozenset[str] = frozenset(),
) -> tuple[SleeveView, ...]:
    views = []
    for spec in cfg.sleeves:  # config order, so the report reads like the config
        ledger = ledgers[spec.sleeve_id]
        fold_complete = spec.sleeve_id not in broken
        rows = []
        complete = fold_complete
        for symbol, (qty, avg_cost) in sorted(ledger.positions.items()):
            if qty == 0:
                continue
            # A broken fold gets no marks: pricing quantities the journal no
            # longer supports would assert a P&L the evidence does not.
            mark = marks.get(symbol) if fold_complete else None
            if mark is None:
                complete = False
            rows.append(
                PositionRow(
                    symbol=symbol,
                    qty=qty,
                    avg_cost=avg_cost,
                    mark=mark,
                    market_value=qty * mark if mark is not None else None,
                    open_pnl=(mark - avg_cost) * qty if mark is not None else None,
                )
            )
        equity = None
        if complete:
            equity = ledger.cash + sum(
                (r.market_value for r in rows if r.market_value is not None), Decimal("0")
            )
        views.append(
            SleeveView(
                sleeve_id=spec.sleeve_id,
                capital=spec.capital,
                cash=ledger.cash,
                positions=tuple(rows),
                equity=equity,
                fold_complete=fold_complete,
            )
        )
    return tuple(views)


def _fill_portion_bound(
    conn: sqlite3.Connection, coid: str | None, tables: set[str]
) -> int | None:
    """Max ledger entries one broker-fill audit row can explain: 1 without a
    net plan, else the plan's residual leg count. None = cannot bound."""
    if coid is None or "paper_orders" not in tables:
        return None
    row = conn.execute(
        "SELECT net_plan_json FROM paper_orders WHERE client_order_id = ?", (coid,)
    ).fetchone()
    if row is None:
        return None
    if not row[0]:
        return 1
    try:
        legs = json.loads(row[0]).get("residual_legs")
    except (ValueError, AttributeError):
        return None
    return max(1, len(legs)) if isinstance(legs, list) else None


def read_activity(conn: sqlite3.Connection, since: datetime) -> ActivityView:
    counts: dict[str, int] = {}
    rejected_orders = 0
    rejections: dict[str, int] = {}
    reconcile_pass_ts: set[str] = set()
    reconciles_without_ts = 0
    fill_coids: list[str | None] = []
    capacity = 0
    capacity_unbounded = False
    tables = _tables(conn)
    if "audit" in tables:
        rows = conn.execute(
            "SELECT kind, payload FROM audit WHERE ts >= ? ORDER BY seq",
            (since.isoformat(),),
        ).fetchall()
        for kind, payload_json in rows:
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "order":
                payload = json.loads(payload_json)
                if payload.get("state") == "rejected":
                    rejected_orders += 1
            elif kind == "verdict":
                payload = json.loads(payload_json)
                if payload.get("decision") == "reject":
                    # The stored verdict has no flat reason list; recover it from
                    # the per-check results, exactly as the governor derived it.
                    for result in payload.get("results", []):
                        if result.get("decision") == "reject" and result.get("reason"):
                            reason = result["reason"]
                            rejections[reason] = rejections.get(reason, 0) + 1
            elif kind == "reconcile":
                # One pass, two rows: ReconcileEngine audits the report and
                # reconcile_and_arm audits the same report again. The report's
                # own ts is the pass identity; a row without one cannot be
                # proven a duplicate, so it counts as its own pass.
                payload = json.loads(payload_json)
                inner_ts = payload.get("ts")
                if isinstance(inner_ts, str):
                    reconcile_pass_ts.add(inner_ts)
                else:
                    reconciles_without_ts += 1
            elif kind == "fill":
                payload = json.loads(payload_json)
                coid = payload.get("coid")
                fill_coids.append(coid if isinstance(coid, str) else None)
            elif kind == "cross":
                payload = json.loads(payload_json)
                legs = payload.get("legs")
                if isinstance(legs, int) and legs >= 0:
                    capacity += legs
                else:
                    capacity_unbounded = True
    for coid in fill_coids:
        bound = _fill_portion_bound(conn, coid, tables)
        if bound is None:
            capacity_unbounded = True
            break
        capacity += bound
    return ActivityView(
        since=since,
        audit_counts=counts,
        reconcile_events=len(reconcile_pass_ts) + reconciles_without_ts,
        orders_submitted=counts.get("order", 0),
        orders_rejected=rejected_orders,
        rejections_by_reason=rejections,
        fills_applied=counts.get("fill", 0),
        fill_explain_capacity=None if capacity_unbounded else capacity,
    )


# -- the composed snapshot ---------------------------------------------------


def gather(
    conn: sqlite3.Connection,
    cfg: ObserverConfig,
    now: datetime,
    db_path: Path | str,
) -> Snapshot:
    gaps: list[str] = []
    midnight = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)

    state = read_system_state(conn)
    latches = read_unacked_latches(conn, now)
    alerts = read_unacked_alerts(conn, now)
    heartbeat = read_heartbeat(conn, now)
    pending = read_pending_commands(conn)
    equity = read_equity(conn)
    activity = read_activity(conn, midnight)

    ledger_rows = read_ledger_rows(conn)
    fills_today = fills_since(ledger_rows, midnight)
    fills_yesterday = tuple(
        f for f in fills_since(ledger_rows, midnight - timedelta(days=1)) if f.ts < midnight
    )
    ledgers, fold_gaps, broken = fold_ledgers(ledger_rows, cfg)
    gaps.extend(fold_gaps)

    held = {
        symbol
        for ledger in ledgers.values()
        for symbol, (qty, _avg) in ledger.positions.items()
        if qty != 0
    }
    marks, mark_gaps = last_stored_closes(cfg, held) if held else ({}, [])
    gaps.extend(mark_gaps)
    sleeves = sleeve_views(ledgers, cfg, marks, broken)

    # Fills that reached a ledger today with no fill/cross audit row recording
    # their arrival: the book moved and the trail does not say how. Beyond
    # total absence, the audit rows bound how many entries they CAN explain
    # (a fill row: its net plan's residual legs, or one; a cross row: its
    # legs); a book that moved more than that is partially unexplained.
    if fills_today:
        if not activity.audit_counts.get("fill") and not activity.audit_counts.get("cross"):
            gaps.append(
                f"Cannot explain {len(fills_today)} ledger fill(s) applied today from"
                " evidence: no fill or cross audit row records their arrival"
            )
        elif (
            activity.fill_explain_capacity is not None
            and len(fills_today) > activity.fill_explain_capacity
        ):
            gaps.append(
                f"Cannot explain {len(fills_today) - activity.fill_explain_capacity} of"
                f" {len(fills_today)} ledger fill(s) applied today from evidence: the"
                " fill and cross audit rows account for at most"
                f" {activity.fill_explain_capacity}"
            )

    empty = (
        state is None
        and heartbeat is None
        and not latches
        and not alerts
        and not ledger_rows
        and not equity
        and not activity.audit_counts
        and pending == 0
    )
    return Snapshot(
        generated_at=now,
        db_path=str(db_path),
        empty=empty,
        state=state,
        latches=latches,
        alerts=alerts,
        heartbeat=heartbeat,
        pending_commands=pending,
        fills_today=fills_today,
        fills_yesterday=fills_yesterday,
        sleeves=sleeves,
        equity=equity,
        activity=activity,
        gaps=tuple(gaps),
    )
