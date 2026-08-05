"""Attended paper-trading cycle: the same strategy/sizing/netting code as the
backtester, pointed at a real broker through the real RiskGovernor.

v1 operating model (Phase 3, attended): a human runs `cycle` after the open
(signals from completed daily bars, execution priced off live quotes) and
`poll` later to collect fills. The always-on daemon + watchdog arrive in
Phase 4 — until then, DAY time-in-force and server-side buying-power checks
bound the unattended surface.

Persistence: sleeve ledgers are event-sourced in SQLite (`ledger_entries`);
every cycle folds entries -> ledgers, reconciles against the broker, and only
proceeds if reconciliation passes. Internal crosses execute immediately at the
quote reference (deviation from the backtest's next-bar-open — documented,
conservative at daily cadence, and both legs share one price).
"""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from nwt_contracts import (
    OrderIntent,
    OrderRef,
    PortfolioView,
    RiskContext,
    SessionInfo,
    Side,
    TradingState,
)
from nwt_engine.broker.base import Broker
from nwt_engine.domain import OrderTicket, Universe
from nwt_engine.execution import PositionSizer
from nwt_engine.sleeves import LedgerEntry, SleeveLedger
from nwt_engine.sleeves.allocator import NetPlan, allocate_fill, build_net_plans
from nwt_engine.strategies import BaseStrategy, HistoryView, StrategyContext, get_strategy

from .breakers.monitors import BreakerEvent, CircuitBreakers
from .config import RiskConfig
from .context import GovernorContext, QuoteView, RecentOrder
from .governor import RiskGovernor
from .reasons import ReasonCode
from .protect import plan_protection
from .reconcile import ExpectedState, ReconcileEngine
from .state import TradingStateMachine

# Exit interlock: how long to wait for a protective cancel to confirm before
# deferring the exit to the next cycle. Bounded — never sell into an
# unconfirmed cancel, and never stall the whole cycle for more than ~5s.
_CANCEL_CONFIRM_ATTEMPTS = 5
_CANCEL_CONFIRM_WAIT_S = 1.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sleeve_id TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_orders (
    client_order_id TEXT PRIMARY KEY,
    sleeve_or_net TEXT NOT NULL,
    net_plan_json TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT,
    notional TEXT,
    limit_price TEXT,
    submitted_ts TEXT NOT NULL,
    state TEXT NOT NULL,
    is_entry INTEGER NOT NULL DEFAULT 1,
    is_protective INTEGER NOT NULL DEFAULT 0,
    stop_price TEXT,
    protects_sleeve TEXT
);
CREATE TABLE IF NOT EXISTS fills_seen (
    fill_id TEXT PRIMARY KEY,
    applied_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_open_equity (
    day TEXT PRIMARY KEY,
    equity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class SleeveSpec(BaseModel, frozen=True):
    sleeve_id: str
    strategy: str
    capital: Decimal
    params: dict = {}


class PaperConfig(BaseModel, frozen=True):
    """config/paper.yaml — the attended paper deployment definition.

    unallocated_buffer_usd: cash in the account deliberately outside every
    sleeve; expected cash at reconcile = sum(sleeve cash) + buffer, so the
    Alpaca paper account balance must equal sum(capitals) + buffer exactly
    (reset the paper account to that figure before the first cycle).
    """

    universe_files: list[Path]
    data_root: Path = Path("data/parquet")
    data_provider: str = "alpaca"
    unallocated_buffer_usd: Decimal = Decimal("0")
    sleeves: list[SleeveSpec]

    @classmethod
    def load(cls, path: Path | str) -> "PaperConfig":
        import yaml

        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


class CycleReport(BaseModel, frozen=True):
    ts: datetime
    state: TradingState
    reconciled: bool
    proposals: int
    intents: int
    approved: int
    submitted: int
    rejected_reasons: dict[str, int]
    crosses_executed: int
    notes: tuple[str, ...] = ()


class PaperStore:
    def __init__(self, path: Path | str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        # Live dbs predate the protective columns; ALTER is idempotent-by-try.
        for ddl in (
            "ALTER TABLE paper_orders ADD COLUMN is_protective INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE paper_orders ADD COLUMN stop_price TEXT",
            "ALTER TABLE paper_orders ADD COLUMN protects_sleeve TEXT",
        ):
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists

    def audit(self, now: datetime, kind: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO audit (ts, kind, payload) VALUES (?,?,?)",
            (now.isoformat(), kind, json.dumps(payload, sort_keys=True, default=str)),
        )
        self.conn.commit()

    def apply_entry(self, sleeve_id: str, entry: LedgerEntry, ledger: SleeveLedger) -> None:
        ledger.apply(entry)
        self.conn.execute(
            "INSERT INTO ledger_entries (sleeve_id, entry_json, ts) VALUES (?,?,?)",
            (sleeve_id, entry.model_dump_json(), entry.ts.isoformat()),
        )
        self.conn.commit()

    def fold_ledgers(self, specs: list[SleeveSpec]) -> dict[str, SleeveLedger]:
        ledgers = {
            spec.sleeve_id: SleeveLedger(spec.sleeve_id, spec.capital) for spec in specs
        }
        rows = self.conn.execute(
            "SELECT sleeve_id, entry_json FROM ledger_entries ORDER BY entry_id"
        ).fetchall()
        for sleeve_id, entry_json in rows:
            if sleeve_id in ledgers:
                ledgers[sleeve_id].apply(LedgerEntry.model_validate_json(entry_json))
        return ledgers

    def open_order_rows(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT client_order_id, sleeve_or_net, net_plan_json, symbol, side, qty,"
            " notional, limit_price, submitted_ts, is_protective, stop_price"
            " FROM paper_orders WHERE state='open'"
        ).fetchall()
        keys = [
            "client_order_id",
            "sleeve_or_net",
            "net_plan_json",
            "symbol",
            "side",
            "qty",
            "notional",
            "limit_price",
            "submitted_ts",
            "is_protective",
            "stop_price",
        ]
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def recent_orders(self, since: datetime) -> list[RecentOrder]:
        rows = self.conn.execute(
            "SELECT submitted_ts, symbol, side, sleeve_or_net, is_entry, is_protective"
            " FROM paper_orders WHERE submitted_ts >= ?",
            (since.isoformat(),),
        ).fetchall()
        return [
            RecentOrder(
                ts=datetime.fromisoformat(ts),
                symbol=symbol,
                side=Side(side),
                sleeve_id=sleeve,
                is_entry=bool(is_entry),
                is_protective=bool(is_protective),
            )
            for ts, symbol, side, sleeve, is_entry, is_protective in rows
        ]

    def record_order(
        self,
        coid: str,
        sleeve_or_net: str,
        plan: NetPlan | None,
        symbol: str,
        side: Side,
        qty: Decimal | None,
        notional: Decimal | None,
        limit_price: Decimal | None,
        now: datetime,
        is_entry: bool,
        *,
        is_protective: bool = False,
        stop_price: Decimal | None = None,
    ) -> None:
        self.conn.execute(
            # Explicit columns: a bare VALUES list silently corrupts the row
            # the day the schema gains a column (protective stops will add three).
            "INSERT OR REPLACE INTO paper_orders (client_order_id, sleeve_or_net,"
            " net_plan_json, symbol, side, qty, notional, limit_price,"
            " submitted_ts, state, is_entry, is_protective, stop_price,"
            " protects_sleeve) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                coid,
                sleeve_or_net,
                plan.model_dump_json() if plan else None,
                symbol,
                side.value,
                str(qty) if qty is not None else None,
                str(notional) if notional is not None else None,
                str(limit_price) if limit_price is not None else None,
                now.isoformat(),
                "open",
                1 if is_entry else 0,
                1 if is_protective else 0,
                str(stop_price) if stop_price is not None else None,
                sleeve_or_net if is_protective else None,
            ),
        )
        self.conn.commit()

    def mark_order(self, coid: str, state: str) -> None:
        self.conn.execute(
            "UPDATE paper_orders SET state=? WHERE client_order_id=?", (state, coid)
        )
        self.conn.commit()

    def fill_seen(self, fill_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM fills_seen WHERE fill_id=?", (fill_id,)
            ).fetchone()
            is not None
        )

    def mark_fill(self, fill_id: str, now: datetime) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO fills_seen VALUES (?,?)", (fill_id, now.isoformat())
        )
        self.conn.commit()

    def day_open(self, day: str, current_equity: Decimal) -> Decimal:
        row = self.conn.execute(
            "SELECT equity FROM day_open_equity WHERE day=?", (day,)
        ).fetchone()
        if row:
            return Decimal(row[0])
        self.conn.execute(
            "INSERT INTO day_open_equity VALUES (?,?)", (day, str(current_equity))
        )
        self.conn.commit()
        return current_equity


def deterministic_coid(prefix: str, symbol: str, side: Side, as_of: datetime) -> str:
    digest = hashlib.sha256(
        f"{prefix}|{symbol}|{side.value}|{as_of.date().isoformat()}".encode()
    ).hexdigest()[:14]
    return f"nwt-{prefix}-{symbol.replace('/', '-')}-{digest}"


class PaperCycle:
    def __init__(
        self,
        broker: Broker,
        universe: Universe,
        sleeves: list[SleeveSpec],
        risk_config: RiskConfig,
        store: PaperStore,
        state_machine: TradingStateMachine,
        breakers: CircuitBreakers,
        governor: RiskGovernor,
        reconciler: ReconcileEngine,
        bars_loader: Callable[[], dict],       # symbol -> list[Bar] (completed dailies)
        quotes_loader: Callable[[], dict],     # symbol -> QuoteView kwargs dict
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        unallocated_buffer: Decimal = Decimal("0"),
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        import time as _time

        self.sleep_fn = sleep_fn or _time.sleep
        self.unallocated_buffer = unallocated_buffer
        self.broker = broker
        self.universe = universe
        self.sleeve_specs = sleeves
        self.cfg = risk_config
        self.store = store
        self.state = state_machine
        self.breakers = breakers
        self.governor = governor
        self.reconciler = reconciler
        self.bars_loader = bars_loader
        self.quotes_loader = quotes_loader
        self.now_fn = now_fn
        self._strategies: dict[str, tuple[BaseStrategy, object]] = {}
        for spec in sleeves:
            cls = get_strategy(spec.strategy)
            params = cls.params_model.model_validate(spec.params)
            self._strategies[spec.sleeve_id] = (cls(), params)

    # -- reconciliation ------------------------------------------------------

    def _expected(self, ledgers: dict[str, SleeveLedger]) -> ExpectedState:
        cash = self.unallocated_buffer + sum(
            (ledger.cash for ledger in ledgers.values()), Decimal("0")
        )
        positions: dict[str, Decimal] = {}
        for ledger in ledgers.values():
            for symbol, (qty, _avg) in ledger.positions.items():
                if qty != 0:
                    positions[symbol] = positions.get(symbol, Decimal("0")) + qty
        open_ids = {row["client_order_id"] for row in self.store.open_order_rows()}
        return ExpectedState(cash=cash, positions=positions, open_client_order_ids=open_ids)

    def reconcile_and_arm(self, ledgers: dict[str, SleeveLedger]) -> bool:
        report = self.reconciler.reconcile(self._expected(ledgers))
        self.store.audit(self.now_fn(), "reconcile", report.model_dump(mode="json"))
        if report.ok:
            self.state.mark_reconciled()
            return True
        self.state.trip(
            "reconcile_mismatch",
            TradingState.HALTED,
            ReasonCode.RECONCILE_MISMATCH,
            "; ".join(report.unexplained)[:400],
        )
        return False

    # -- fills ---------------------------------------------------------------

    def poll_fills(self, ledgers: dict[str, SleeveLedger]) -> int:
        applied = 0
        now = self.now_fn()
        open_rows = {row["client_order_id"]: row for row in self.store.open_order_rows()}
        for fill in self.broker.drain_events():
            if self.store.fill_seen(fill.fill_id):
                continue
            row = open_rows.get(fill.client_order_id)
            if row is None:
                self.store.audit(
                    now, "external_fill", {"fill_id": fill.fill_id, "coid": fill.client_order_id}
                )
                continue
            stop_fired = bool(row["is_protective"])
            if stop_fired:
                # A catastrophe stop EXECUTED: by construction an event outside
                # the entire sample (owner decision §8 row 5). Bound the blast:
                # 24h cooldown via stop_out above, and HALT the whole system —
                # whatever the market is doing, it is not what the strategies
                # were validated on. Trip BEFORE applying the fill so a crash
                # mid-poll leaves us halted, not un-halted with a stop fill.
                self.store.audit(
                    now,
                    "stop_fired",
                    {"coid": fill.client_order_id, "symbol": fill.symbol,
                     "price": str(fill.price), "qty": str(fill.qty)},
                )
                if self.cfg.protection.halt_on_fire:
                    self.state.trip(
                        "protection",
                        TradingState.HALTED,
                        ReasonCode.STOP_FIRED,
                        f"catastrophe stop filled: {fill.symbol} {fill.qty} @"
                        f" {fill.price} ({fill.client_order_id})"[:200],
                    )
            if row["net_plan_json"]:
                plan = NetPlan.model_validate_json(row["net_plan_json"])
                portions = [
                    (a.sleeve_id, a.qty, a.fees)
                    for a in allocate_fill(plan, fill.qty, fill.fees)
                ]
            else:
                portions = [(row["sleeve_or_net"], fill.qty, fill.fees)]
            for sleeve_id, qty, fees in portions:
                ledger = ledgers[sleeve_id]
                held_before = ledger.position_qty(fill.symbol)
                avg_before = ledger.positions.get(
                    fill.symbol, (Decimal("0"), Decimal("0"))
                )[1]
                self.store.apply_entry(
                    sleeve_id,
                    LedgerEntry(
                        kind="fill",
                        ts=fill.ts,
                        symbol=fill.symbol,
                        side=fill.side,
                        qty=qty,
                        price=fill.price,
                        fees=fees,
                    ),
                    ledger,
                )
                if fill.side is Side.SELL and held_before == qty:
                    pnl = (fill.price - avg_before) * qty - fees
                    self.breakers.observe(
                        BreakerEvent(
                            kind="round_trip",
                            ts=now,
                            payload={
                                "symbol": fill.symbol,
                                "pnl": str(pnl),
                                "stop_out": stop_fired,
                            },
                        )
                    )
            self.store.mark_fill(fill.fill_id, now)
            self.store.mark_order(fill.client_order_id, "filled")
            self.store.audit(now, "fill", {"fill_id": fill.fill_id, "coid": fill.client_order_id})
            applied += 1
        return applied

    # -- the cycle -----------------------------------------------------------

    def preflight(self, marks: dict[str, Decimal]) -> list[str]:
        """Config-vs-reality sanity checks run before every cycle.

        The whole-share floor is the one that bites: equities trade in whole
        shares, so any symbol priced above the per-order notional cap can never
        produce a legal order — the sleeve holding it silently never trades.
        Caught in the first live rehearsal (SPY at $746 vs a $500 cap).
        """
        problems: list[str] = []
        global_cap = self.cfg.order.max_notional_usd
        for inst in self.universe.instruments:
            if inst.fractionable:
                continue
            price = marks.get(inst.symbol)
            if price is None:
                continue
            if price > global_cap:
                problems.append(
                    f"{inst.symbol} at {price} exceeds order.max_notional_usd "
                    f"{global_cap}: one whole share is unbuyable, orders always reject"
                )
        # Per-sleeve caps have the same floor, checked against what each sleeve
        # can actually reach: the cheapest whole share in its own universe.
        for spec in self.sleeve_specs:
            budget = self.cfg.sleeves.get(spec.sleeve_id)
            if budget is None:
                problems.append(f"sleeve {spec.sleeve_id} has no risk budget configured")
                continue
            reachable = [
                (inst.symbol, marks[inst.symbol])
                for inst in self.universe.instruments
                if not inst.fractionable and inst.symbol in marks
            ]
            unbuyable = [s for s, p in reachable if p > budget.max_order_usd]
            if unbuyable and len(unbuyable) == len(reachable):
                problems.append(
                    f"sleeve {spec.sleeve_id}: max_order_usd {budget.max_order_usd} is "
                    f"below every share price in the universe — it can never trade"
                )
            elif unbuyable:
                problems.append(
                    f"sleeve {spec.sleeve_id}: max_order_usd {budget.max_order_usd} "
                    f"cannot buy one share of {', '.join(sorted(unbuyable))}"
                )
        return problems

    def run_cycle(self) -> CycleReport:
        now = self.now_fn()
        notes: list[str] = []
        ledgers = self.store.fold_ledgers(self.sleeve_specs)

        # Poll BEFORE reconciling. A fill that lands between cycles (today: a
        # partial from yesterday; soon: a resting GTC stop firing overnight)
        # moves the broker's book but not ours — reconciling first sees that
        # honest gap as a mismatch, HALTs, and the early-return strands the
        # fill forever. Collect what the broker already did, then judge.
        # (Scheduler._collect_and_reconcile has always done it in this order.)
        self.poll_fills(ledgers)

        reconciled = self.reconcile_and_arm(ledgers)
        if not reconciled:
            return CycleReport(
                ts=now,
                state=self.state.state(),
                reconciled=False,
                proposals=0,
                intents=0,
                approved=0,
                submitted=0,
                rejected_reasons={},
                crosses_executed=0,
                notes=("reconciliation failed — HALTED",),
            )
        account = self.broker.get_account()
        day_open = self.store.day_open(now.date().isoformat(), account.equity)
        self.breakers.observe(
            BreakerEvent(
                kind="equity",
                ts=now,
                payload={"equity": str(account.equity), "day_open_equity": str(day_open)},
            )
        )
        self.breakers.tick()

        bars_by_symbol = self.bars_loader()
        history = HistoryView(bars_by_symbol, now)
        marks: dict[str, Decimal] = {}
        for symbol in self.universe.symbols:
            close = history.last_close(symbol)
            if close is not None:
                marks[symbol] = close

        quote_views: dict[str, QuoteView] = {}
        for symbol, kwargs in self.quotes_loader().items():
            quote_views[symbol] = QuoteView(symbol=symbol, **kwargs)
            marks[symbol] = quote_views[symbol].last

        for problem in self.preflight(marks):
            notes.append(f"PREFLIGHT: {problem}")
            self.store.audit(now, "preflight_problem", {"detail": problem})

        # Strategy decisions from ledger truth.
        proposals = []
        sleeve_views: list[PortfolioView] = []
        for spec in self.sleeve_specs:
            ledger = ledgers[spec.sleeve_id]
            view = ledger.snapshot(now, {s: m for s, m in marks.items()})
            sleeve_views.append(view)
            strategy, params = self._strategies[spec.sleeve_id]
            ctx = StrategyContext(now, view, history, params)
            for proposal in strategy.on_schedule(ctx):
                proposals.append(proposal)
                self.store.audit(
                    now,
                    "proposal",
                    {
                        "sleeve": spec.sleeve_id,
                        "strategy": proposal.strategy,
                        "action": proposal.action.model_dump(mode="json"),
                    },
                )

        # Live quotes price the orders; bars still drive the signals above.
        sizer = PositionSizer(
            self.universe,
            reference_prices={s: q.last for s, q in quote_views.items()},
        )
        intents = []
        for spec in self.sleeve_specs:
            sleeve_proposals = [p for p in proposals if p.sleeve_id == spec.sleeve_id]
            if sleeve_proposals:
                intents.extend(
                    sizer.size(sleeve_proposals, ledgers[spec.sleeve_id], history, now)
                )

        broker_open = self.broker.get_open_orders()
        rows_by_coid = {r["client_order_id"]: r for r in self.store.open_order_rows()}
        open_refs = []
        for status in broker_open:
            row = rows_by_coid.get(status.client_order_id)
            if row is None:
                continue  # unknown broker order — reconcile reports it as EXTERNAL
            open_refs.append(
                OrderRef(
                    client_order_id=status.client_order_id,
                    symbol=row["symbol"],
                    side=Side(row["side"]),
                    qty=Decimal(row["qty"]) if row["qty"] else None,
                    notional=Decimal(row["notional"]) if row["notional"] else None,
                    limit_price=Decimal(row["limit_price"]) if row["limit_price"] else None,
                    stop_price=Decimal(row["stop_price"]) if row["stop_price"] else None,
                    is_protective=bool(row["is_protective"]),
                    submitted_at=datetime.fromisoformat(row["submitted_ts"]),
                )
            )
        open_refs = tuple(open_refs)

        base_ctx = RiskContext(
            ts=now,
            mode="paper",
            trading_state=self.state.state(),
            account=PortfolioView(
                scope="account", ts=now, cash=account.cash, equity=account.equity
            ),
            sleeves=tuple(sleeve_views),
            open_orders=open_refs,
            sessions=self._sessions(),
            last_reconcile_age_s=0.0,
        )
        gov_ctx = GovernorContext(
            base=base_ctx,
            quotes=quote_views,
            recent_orders=tuple(self.store.recent_orders(now - timedelta(hours=24))),
            open_orders=open_refs,
            cooldowns=tuple(self.breakers.cooldowns()),
            adv_by_symbol=self._adv(bars_by_symbol),
            clock_skew_s=self._clock_skew(now),
        )

        # Protective coverage: desired-vs-resting diff (risk/protect.py). The
        # reconciler is declarative — it never asks WHY coverage is missing
        # (watchdog cancel, kill, 90-day expiry, crash, pre-feature position),
        # it just re-arms the difference every cycle. Its intents run through
        # the same governor as everything else; HALTED admits arm-only.
        protection_plan = plan_protection(ledgers, open_refs, self.universe, self.cfg)
        halted = self.state.state() is TradingState.HALTED
        protective_intents = []
        for stop in protection_plan.arms:
            protective_intents.append(
                OrderIntent(
                    intent_id=stop.client_order_id,
                    sleeve_id=stop.sleeve_id,
                    strategy="protection",
                    symbol=stop.symbol,
                    asset_class=self.universe.get(stop.symbol).asset_class,
                    side=Side.SELL,
                    qty=stop.qty,
                    stop_price=stop.stop_price,
                    as_of=now,
                    created_at=now,
                    reduces_position=True,
                    is_protective=True,
                    provenance="control",
                )
            )
        # Cancels re-price drifted stops (lot changed). In HALTED: arm-only —
        # never cancel (owner decision §8 row 4). To avoid double-covering a
        # symbol whose stale stop we may not cancel, defer its re-arm too.
        if halted:
            frozen = {
                (ref.symbol) for ref in open_refs if ref.is_protective
            }
            protective_intents = [
                i for i in protective_intents if i.symbol not in frozen
            ]
        else:
            for coid in protection_plan.cancels:
                try:
                    self.broker.cancel(coid)
                    self.store.mark_order(coid, "cancelled")
                    self.store.audit(now, "protective_cancel", {"coid": coid})
                except Exception as exc:
                    notes.append(f"protective cancel failed for {coid}: {exc}")

        outcome = self.governor.review(intents + protective_intents, gov_ctx)
        for verdict in outcome.verdicts:
            self.store.audit(now, "verdict", verdict.model_dump(mode="json"))
        rejected: dict[str, int] = {}
        for verdict in outcome.verdicts:
            if verdict.decision == "reject":
                for reason in verdict.reject_reasons:
                    rejected[reason.value] = rejected.get(reason.value, 0) + 1

        submitted = 0
        crosses = 0
        approved_list = list(outcome.approved)

        # Protective stops — direct, NEVER netted: a netted stop has no owning
        # sleeve and allocate_fill would mis-attribute the exit.
        protective_approvals = [a for a in approved_list if a.intent.is_protective]
        approved_total = len(approved_list)  # report the true count, not post-filter
        approved_list = [a for a in approved_list if not a.intent.is_protective]
        for approval in protective_approvals:
            intent = approval.intent
            submitted += self._submit(
                intent.intent_id,  # the deterministic prot- coid
                intent.sleeve_id,
                None,
                intent.symbol,
                Side.SELL,
                approval.approved_qty,
                None,
                None,
                False,
                now,
                stop_price=intent.stop_price,
                is_protective=True,
            )

        # Notional (crypto) flow — per sleeve, unnetted.
        for approval in approved_list:
            if approval.intent.qty is None:
                coid = deterministic_coid(
                    approval.intent.sleeve_id,
                    approval.intent.symbol,
                    approval.intent.side,
                    now,
                )
                submitted += self._submit(
                    coid,
                    approval.intent.sleeve_id,
                    None,
                    approval.intent.symbol,
                    approval.intent.side,
                    None,
                    approval.approved_notional,
                    approval.intent.limit_price,
                    not approval.intent.reduces_position,
                    now,
                )

        # Qty flow — netted.
        for plan in build_net_plans(approved_list):
            if plan.crosses:
                reference = quote_views.get(plan.symbol)
                if reference is None:
                    notes.append(f"no quote for cross on {plan.symbol}; crosses skipped")
                else:
                    from nwt_engine.sleeves.allocator import cross_price

                    price = cross_price(plan, reference.reference)
                    for cross in plan.crosses:
                        self.store.apply_entry(
                            cross.sleeve_id,
                            LedgerEntry(
                                kind="fill",
                                ts=now,
                                symbol=plan.symbol,
                                side=cross.side,
                                qty=cross.qty,
                                price=price,
                                fees=Decimal("0"),
                            ),
                            ledgers[cross.sleeve_id],
                        )
                        crosses += 1
                    self.store.audit(
                        now,
                        "cross",
                        {"symbol": plan.symbol, "price": str(price), "legs": len(plan.crosses)},
                    )
            for leg in plan.unnetted_legs:
                coid = deterministic_coid(leg.sleeve_id, plan.symbol, leg.side, now)
                submitted += self._submit(
                    coid,
                    leg.sleeve_id,
                    None,
                    plan.symbol,
                    leg.side,
                    leg.qty,
                    None,
                    leg.limit_price,
                    leg.side is Side.BUY,
                    now,
                )
            if plan.net_side is not None and plan.net_qty > 0:
                target = (
                    plan.residual_legs[0].sleeve_id
                    if len(plan.residual_legs) == 1
                    else "netted"
                )
                coid = deterministic_coid("net", plan.symbol, plan.net_side, now)
                submitted += self._submit(
                    coid,
                    target,
                    plan if target == "netted" else None,
                    plan.symbol,
                    plan.net_side,
                    plan.net_qty,
                    None,
                    plan.net_limit,
                    plan.net_side is Side.BUY,
                    now,
                )

        self.poll_fills(ledgers)
        return CycleReport(
            ts=now,
            state=self.state.state(),
            reconciled=True,
            proposals=len(proposals),
            intents=len(intents),
            approved=approved_total,
            submitted=submitted,
            rejected_reasons=rejected,
            crosses_executed=crosses,
            notes=tuple(notes),
        )

    def _submit(
        self,
        coid: str,
        sleeve_or_net: str,
        plan: NetPlan | None,
        symbol: str,
        side: Side,
        qty: Decimal | None,
        notional: Decimal | None,
        limit_price: Decimal | None,
        is_entry: bool,
        now: datetime,
        *,
        stop_price: Decimal | None = None,
        is_protective: bool = False,
    ) -> int:
        # EXIT INTERLOCK (design §4 phase 5): a resting sell stop holds the
        # shares, so a strategy SELL on the same symbol would be rejected for
        # insufficient qty — five of those in ten minutes trips the rejection
        # breaker and HALTs on a normal rebalance. Cancel the resting stops,
        # CONFIRM the cancel terminal, then submit. On no confirmation: skip
        # this exit this cycle and audit — never sell into an unconfirmed
        # cancel. The seconds-long unprotected gap is audited; the next
        # cycle's reconciler re-arms whatever remains.
        if side is Side.SELL and not is_protective:
            if not self._clear_protective_stops(symbol, now):
                self.store.audit(
                    now,
                    "exit_deferred",
                    {"coid": coid, "symbol": symbol,
                     "reason": "protective cancel unconfirmed"},
                )
                return 0
        if is_protective:
            ticket = OrderTicket(
                client_order_id=coid,
                symbol=symbol,
                side=side,
                qty=qty,
                order_type="stop",
                stop_price=stop_price,
                # Protection that expires at the close is not protection
                # (GTC-survives-close proven in paper, P0.6a, 2026-08-05).
                tif="gtc",
            )
        else:
            ticket = OrderTicket(
                client_order_id=coid,
                symbol=symbol,
                side=side,
                qty=qty,
                notional=notional,
                limit_price=limit_price,
                # Per-instrument: `day` for equities (nothing survives the close,
                # which bounds the unattended surface), `gtc` for crypto — Alpaca
                # rejects `day` on a 24/7 market that never has a close to expire at.
                tif=self.universe.get(symbol).tif,
            )
        self.store.record_order(
            coid, sleeve_or_net, plan, symbol, side, qty, notional, limit_price, now,
            is_entry, is_protective=is_protective, stop_price=stop_price,
        )
        ack = self.broker.submit(ticket)
        self.store.audit(
            now,
            "order",
            {"coid": coid, "state": ack.state.value, "reason": ack.reason, "symbol": symbol},
        )
        if ack.state.value == "rejected":
            self.store.mark_order(coid, "rejected")
            self.breakers.observe(BreakerEvent(kind="rejection", ts=now, payload={}))
            return 0
        return 1

    def _clear_protective_stops(self, symbol: str, now: datetime) -> bool:
        """Cancel every resting protective stop on `symbol`; True when clear.

        Over-cancels on purpose (all sleeves' stops on the symbol, not just the
        seller's): an under-cancel rejects the exit and feeds the rejection
        breaker, while an over-cancel is healed by the next cycle's re-arm and
        the gap is audited. Confirmation is a bounded poll of open orders —
        never submit a sell against an unconfirmed cancel.
        """
        from .protect import is_protective_coid

        resting = [
            row["client_order_id"]
            for row in self.store.open_order_rows()
            if row["symbol"] == symbol and row["is_protective"]
            and is_protective_coid(row["client_order_id"])
        ]
        if not resting:
            return True
        for coid in resting:
            try:
                self.broker.cancel(coid)
            except Exception as exc:
                self.store.audit(
                    now, "protective_cancel_error", {"coid": coid, "error": str(exc)}
                )
                return False
        for _ in range(_CANCEL_CONFIRM_ATTEMPTS):
            open_now = {o.client_order_id for o in self.broker.get_open_orders()}
            still = [c for c in resting if c in open_now]
            if not still:
                for coid in resting:
                    self.store.mark_order(coid, "cancelled")
                    self.store.audit(
                        now, "protective_cancel", {"coid": coid, "for_exit_on": symbol}
                    )
                return True
            self.sleep_fn(_CANCEL_CONFIRM_WAIT_S)
        return False

    def _adv(self, bars_by_symbol: dict) -> dict[str, Decimal]:
        adv: dict[str, Decimal] = {}
        for symbol, bars in bars_by_symbol.items():
            recent = bars[-20:]
            if recent:
                adv[symbol] = sum((b.volume for b in recent), Decimal("0")) / len(recent)
        return adv

    def _sessions(self) -> tuple[SessionInfo, ...]:
        """XNYS session from the broker clock (truth), crypto always open."""
        sessions = [SessionInfo(calendar="24_7", is_open=True)]
        clock_fn = getattr(self.broker, "clock", None)
        if clock_fn is not None:
            try:
                broker_clock = clock_fn()
                sessions.append(
                    SessionInfo(
                        calendar="XNYS",
                        is_open=bool(broker_clock.get("is_open")),
                        next_open=broker_clock.get("next_open"),
                        next_close=broker_clock.get("next_close"),
                    )
                )
            except Exception:
                pass  # no clock => no XNYS session info => equity orders reject (safe)
        return tuple(sessions)

    def _clock_skew(self, now: datetime) -> float:
        """Clock difference with the round-trip removed.

        Naively comparing the broker's timestamp to local now measures drift
        PLUS network latency, so a slow response is indistinguishable from a
        drifting clock — it rejected a whole cycle on 2026-08-04. Sampling
        local time either side of the call and comparing against the midpoint
        (the classic NTP estimate) leaves only the drift, and the residual
        error is bounded by half the round-trip.
        """
        clock_fn = getattr(self.broker, "clock", None)
        if clock_fn is None:
            return 0.0
        try:
            before = self.now_fn()
            broker_clock = clock_fn()
            after = self.now_fn()
            midpoint = before + (after - before) / 2
            return abs((broker_clock["timestamp"] - midpoint).total_seconds())
        except Exception:
            return 0.0


def build_paper_cycle(
    paper_cfg: PaperConfig,
    risk_cfg: RiskConfig,
    broker: Broker,
    db_path: Path | str,
    quotes_loader: Callable[[], dict],
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PaperCycle:
    """Assemble the full attended-cycle stack from config files."""
    import yaml

    from nwt_engine.data import ParquetStore
    from nwt_engine.domain import Instrument, Timeframe

    from .checks import default_checks
    from .governor import RiskGovernor as _Gov

    instruments = []
    for file in paper_cfg.universe_files:
        raw = yaml.safe_load(Path(file).read_text())
        for entry in raw["instruments"]:
            instruments.append(Instrument.model_validate(entry))
    universe = Universe(name="paper", instruments=tuple(instruments))

    store = PaperStore(db_path)
    state = TradingStateMachine(db_path, "paper", now_fn)
    state.on_startup()
    breakers = CircuitBreakers(risk_cfg.breakers, state, now_fn, db_path)
    governor = _Gov(default_checks(), risk_cfg, state.state, audit=None)
    reconciler = ReconcileEngine(
        broker,
        risk_cfg.reconcile,
        now_fn,
        audit=lambda kind, payload: store.audit(now_fn(), kind, payload),
    )

    parquet = ParquetStore(paper_cfg.data_root)

    def bars_loader() -> dict:
        bars = {}
        for inst in universe.instruments:
            try:
                bars[inst.symbol] = parquet.read_bars(
                    paper_cfg.data_provider, Timeframe.D1, inst.symbol
                )
            except FileNotFoundError:
                bars[inst.symbol] = []
        return bars

    return PaperCycle(
        broker=broker,
        universe=universe,
        sleeves=paper_cfg.sleeves,
        risk_config=risk_cfg,
        store=store,
        state_machine=state,
        breakers=breakers,
        governor=governor,
        reconciler=reconciler,
        bars_loader=bars_loader,
        quotes_loader=quotes_loader,
        now_fn=now_fn,
        unallocated_buffer=paper_cfg.unallocated_buffer_usd,
    )
