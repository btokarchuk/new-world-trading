"""BacktestRunner: wires clock + feed + broker + sleeves + strategies + journal.

Determinism contract: two runs of the same config produce byte-identical event
journals (verified by hash). Everything ordered, every id counter-based, no
wall-clock or randomness anywhere in the loop.

Reconciliation runs at every daily close even in backtest — the sum of sleeve
ledgers must equal the SimBroker's account to the cent, or the run aborts.
This is the same code path live mode will use against Alpaca.
"""

import hashlib
import itertools
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel

from nwt_contracts import PortfolioView, RiskContext, TradingState

from nwt_engine.broker import SimBroker
from nwt_engine.core import BarEvent, EventQueue, ScheduleEvent, SimClock
from nwt_engine.data import HistoricalBarFeed, ParquetStore
from nwt_engine.domain import Bar, CorporateAction, Fill, OrderTicket
from nwt_engine.execution import NullGovernor, PositionSizer
from nwt_engine.sleeves import LedgerEntry, SleeveLedger
from nwt_engine.sleeves.allocator import (
    NetPlan,
    allocate_fill,
    build_net_plans,
    cross_price,
)
from nwt_engine.strategies import BaseStrategy, HistoryView, StrategyContext, get_strategy

from .config import ExperimentConfig
from .db import ResultsDB
from .metrics import compute_metrics, compute_relative_metrics


class ReconciliationError(RuntimeError):
    pass


class SleeveResult(BaseModel):
    sleeve_id: str
    final_equity: Decimal
    metrics: dict[str, float]


class RunResult(BaseModel):
    run_id: str
    journal_hash: str
    sleeves: list[SleeveResult]


_RECONCILE_TOLERANCE = Decimal("0.01")


class BacktestRunner:
    def __init__(self, config: ExperimentConfig) -> None:
        if config.mode != "backtest":
            raise ValueError(
                "BacktestRunner only runs mode=backtest; paper/live require the "
                "risk layer (Phase 3) — NullGovernor is not allowed near real APIs"
            )
        self.config = config
        # Deterministic run id: same config -> same run id -> reruns overwrite.
        cfg_json = config.model_dump_json()
        self.run_id = "run-" + hashlib.sha256(cfg_json.encode()).hexdigest()[:16]
        self._cfg_json = cfg_json

    def run(self) -> RunResult:
        cfg = self.config
        universe = cfg.universe
        store = ParquetStore(cfg.data.root)

        bars_by_symbol: dict[str, list[Bar]] = {}
        corp_actions: list[CorporateAction] = []
        for inst in universe.instruments:
            bars_by_symbol[inst.symbol] = store.read_bars(
                cfg.data.provider, cfg.data.timeframe, inst.symbol
            )
            corp_actions.extend(store.read_corporate_actions(cfg.data.provider, inst.symbol))

        feed = HistoricalBarFeed(bars_by_symbol)
        if not len(feed):
            raise ValueError("no bars for configured instruments")

        all_bars = list(feed)
        clock = SimClock(all_bars[0].ts_close)
        total_cash = sum((s.capital for s in cfg.sleeves), Decimal("0"))
        broker = SimBroker(universe, cfg.costs, total_cash, clock.now)
        broker.load_corporate_actions(corp_actions)

        # Sleeve-side corporate actions (same table, independent application —
        # reconciliation proves they stay in sync with the broker's view).
        pending_ca: dict[str, list[CorporateAction]] = {}
        for action in corp_actions:
            pending_ca.setdefault(action.symbol, []).append(action)

        ledgers: dict[str, SleeveLedger] = {}
        strategies: dict[str, tuple[BaseStrategy, object]] = {}
        for sleeve_cfg in cfg.sleeves:
            ledgers[sleeve_cfg.sleeve_id] = SleeveLedger(sleeve_cfg.sleeve_id, sleeve_cfg.capital)
            strategy_cls = get_strategy(sleeve_cfg.strategy)
            if strategy_cls.contamination_sensitive:
                raise ValueError(
                    f"{sleeve_cfg.strategy} is contamination-sensitive: backtests are "
                    "refused unless ticker-anonymized (see plan; not implemented yet)"
                )
            params = strategy_cls.params_model.model_validate(sleeve_cfg.params)
            strategies[sleeve_cfg.sleeve_id] = (strategy_cls(), params)

        intent_seq = itertools.count(1)
        sizer = PositionSizer(universe, id_factory=lambda: f"intent-{next(intent_seq)}")
        governor = NullGovernor(universe)

        db = ResultsDB(cfg.results_db)
        db.start_run(
            self.run_id, cfg.id, cfg.mode, self._cfg_json, datetime.now(UTC).isoformat()
        )
        journal_seq = itertools.count(1)

        def journal(ts: datetime, type_: str, payload: dict) -> None:
            db.journal(self.run_id, next(journal_seq), ts, type_, payload)

        # Build the queue: every bar, plus one decision point per distinct close ts.
        queue = EventQueue()
        decision_ts = sorted({b.ts_close for b in all_bars})
        for bar in all_bars:
            queue.push(BarEvent(ts=bar.ts_close, bar=bar))
        for ts in decision_ts:
            queue.push(ScheduleEvent(ts=ts, label="daily_close"))

        marks: dict[str, Decimal] = {}
        # client_order_id -> sleeve_id (single-sleeve order) or NetPlan (netted)
        order_alloc: dict[str, str | NetPlan] = {}
        # symbol -> [NetPlan] whose internal crosses await the next bar's open
        pending_crosses: dict[str, list[NetPlan]] = {}
        order_seq = itertools.count(1)
        cross_seq = itertools.count(1)
        cycle = 0

        try:
            while queue:
                event = queue.pop()
                clock.advance_to(event.ts)

                if isinstance(event, BarEvent):
                    self._apply_sleeve_corp_actions(
                        event.bar, pending_ca, ledgers, journal
                    )
                    self._resolve_crosses(
                        event.bar, pending_crosses, ledgers, cross_seq, db, journal
                    )
                    broker.on_bar(event.bar)
                    marks[event.bar.symbol] = event.bar.close
                    journal(
                        event.ts,
                        "bar",
                        {"symbol": event.bar.symbol, "close": str(event.bar.close)},
                    )
                    for fill in broker.drain_events():
                        self._apply_fill(fill, order_alloc, ledgers, db, journal)

                elif isinstance(event, ScheduleEvent):
                    cycle += 1
                    broker.expire_day_orders()
                    self._mark_and_reconcile(event.ts, ledgers, broker, marks, db, journal)
                    self._decide(
                        event.ts,
                        cycle,
                        bars_by_symbol,
                        ledgers,
                        strategies,
                        sizer,
                        governor,
                        broker,
                        order_alloc,
                        pending_crosses,
                        order_seq,
                        db,
                        journal,
                    )
                db.commit()
        except Exception:
            db.finish_run(self.run_id, datetime.now(UTC).isoformat(), "failed")
            db.close()
            raise

        def equity_series(sleeve_id: str) -> list[float]:
            rows = db.conn.execute(
                "SELECT equity FROM equity_daily WHERE run_id=? AND sleeve_id=? ORDER BY ts",
                (self.run_id, sleeve_id),
            ).fetchall()
            return [float(r[0]) for r in rows]

        control_series = (
            equity_series(cfg.control_sleeve)
            if cfg.control_sleeve and cfg.control_sleeve in ledgers
            else None
        )

        results: list[SleeveResult] = []
        for sleeve_id, ledger in ledgers.items():
            series = equity_series(sleeve_id)
            sleeve_metrics = compute_metrics(series)
            if control_series is not None and sleeve_id != cfg.control_sleeve:
                sleeve_metrics.update(compute_relative_metrics(series, control_series))

            # Turnover and cost drag from recorded external fills (crosses excluded:
            # they are fee-free transfers between sleeves, not market activity).
            traded, fees_paid = db.conn.execute(
                "SELECT COALESCE(SUM(CAST(qty AS REAL) * CAST(price AS REAL)), 0),"
                " COALESCE(SUM(CAST(fees AS REAL)), 0) FROM fills"
                " WHERE run_id=? AND sleeve_id=? AND client_order_id != 'internal_cross'",
                (self.run_id, sleeve_id),
            ).fetchone()
            if series:
                avg_equity = sum(series) / len(series)
                years = max(len(series) / 252, 1e-9)
                sleeve_metrics["turnover_annualized"] = (
                    (traded / 2) / avg_equity / years if avg_equity > 0 else 0.0
                )
                sleeve_metrics["fees_total"] = fees_paid
                sleeve_metrics["fee_drag_annualized"] = (
                    fees_paid / avg_equity / years if avg_equity > 0 else 0.0
                )

            for name, value in sleeve_metrics.items():
                db.record_metric(self.run_id, sleeve_id, name, value)
            results.append(
                SleeveResult(
                    sleeve_id=sleeve_id,
                    final_equity=ledger.equity(marks),
                    metrics=sleeve_metrics,
                )
            )
        journal_hash = db.finish_run(self.run_id, datetime.now(UTC).isoformat(), "completed")
        db.close()
        return RunResult(run_id=self.run_id, journal_hash=journal_hash, sleeves=results)

    # -- steps ---------------------------------------------------------------

    def _apply_sleeve_corp_actions(self, bar, pending_ca, ledgers, journal) -> None:
        actions = pending_ca.get(bar.symbol)
        if not actions:
            return
        remaining = []
        for action in actions:
            if action.ex_date <= bar.ts_open:
                for ledger in ledgers.values():
                    qty = ledger.position_qty(bar.symbol)
                    if qty == 0:
                        continue
                    if action.kind == "dividend":
                        amount = qty * action.cash
                        ledger.apply(
                            LedgerEntry(
                                kind="dividend", ts=bar.ts_open, symbol=bar.symbol, cash=amount
                            )
                        )
                        journal(
                            bar.ts_open,
                            "dividend",
                            {
                                "sleeve": ledger.sleeve_id,
                                "symbol": bar.symbol,
                                "amount": str(amount),
                            },
                        )
                    elif action.kind == "split":
                        ledger.apply(
                            LedgerEntry(
                                kind="split", ts=bar.ts_open, symbol=bar.symbol, ratio=action.ratio
                            )
                        )
                        journal(
                            bar.ts_open,
                            "split",
                            {
                                "sleeve": ledger.sleeve_id,
                                "symbol": bar.symbol,
                                "ratio": str(action.ratio),
                            },
                        )
            else:
                remaining.append(action)
        pending_ca[bar.symbol] = remaining

    def _apply_fill(self, fill: Fill, order_alloc, ledgers, db, journal) -> None:
        target = order_alloc.get(fill.client_order_id)
        if target is None:
            raise ReconciliationError(f"fill for unknown order {fill.client_order_id}")

        if isinstance(target, str):
            portions = [(target, fill.qty, fill.fees)]
        else:
            portions = [
                (a.sleeve_id, a.qty, a.fees)
                for a in allocate_fill(target, fill.qty, fill.fees)
            ]

        for index, (sleeve_id, qty, fees) in enumerate(portions):
            ledgers[sleeve_id].apply(
                LedgerEntry(
                    kind="fill",
                    ts=fill.ts,
                    symbol=fill.symbol,
                    side=fill.side,
                    qty=qty,
                    price=fill.price,
                    fees=fees,
                )
            )
            payload = {
                "fill_id": f"{fill.fill_id}-{index}" if len(portions) > 1 else fill.fill_id,
                "client_order_id": fill.client_order_id,
                "symbol": fill.symbol,
                "side": fill.side.value,
                "qty": str(qty),
                "price": str(fill.price),
                "fees": str(fees),
                "ts": fill.ts.isoformat(),
                "sleeve": sleeve_id,
            }
            journal(fill.ts, "fill", payload)
            db.record_fill(self.run_id, sleeve_id, **{k: v for k, v in payload.items() if k != "sleeve"})

    def _resolve_crosses(
        self, bar, pending_crosses, ledgers, cross_seq, db, journal
    ) -> None:
        """Execute internal crosses queued for this symbol at this bar's open.

        Fee-free by design; the price is the bar open clamped into the fair
        interval, so no leg transacts outside its own limit. Sleeve positions
        move; the broker account is untouched — reconciliation still balances
        because the cross nets to zero across sleeves.
        """
        plans = pending_crosses.pop(bar.symbol, None)
        if not plans:
            return
        for plan in plans:
            price = cross_price(plan, bar.open)
            for cross in plan.crosses:
                ledgers[cross.sleeve_id].apply(
                    LedgerEntry(
                        kind="fill",
                        ts=bar.ts_open,
                        symbol=bar.symbol,
                        side=cross.side,
                        qty=cross.qty,
                        price=price,
                        fees=Decimal("0"),
                    )
                )
                fill_id = f"cross-{next(cross_seq)}"
                payload = {
                    "fill_id": fill_id,
                    "client_order_id": None,
                    "symbol": bar.symbol,
                    "side": cross.side.value,
                    "qty": str(cross.qty),
                    "price": str(price),
                    "fees": "0",
                    "ts": bar.ts_open.isoformat(),
                    "sleeve": cross.sleeve_id,
                    "source": "internal_cross",
                }
                journal(bar.ts_open, "fill", payload)
                db.record_fill(
                    self.run_id,
                    cross.sleeve_id,
                    fill_id=fill_id,
                    client_order_id="internal_cross",
                    symbol=bar.symbol,
                    side=cross.side.value,
                    qty=str(cross.qty),
                    price=str(price),
                    fees="0",
                    ts=bar.ts_open.isoformat(),
                )

    def _mark_and_reconcile(self, ts, ledgers, broker, marks, db, journal) -> None:
        account = broker.get_account()
        sleeves_cash = sum((ledger.cash for ledger in ledgers.values()), Decimal("0"))
        sleeves_equity = Decimal("0")
        for ledger in ledgers.values():
            equity = ledger.equity(marks)
            sleeves_equity += equity
            db.record_equity(self.run_id, ledger.sleeve_id, ts, str(ledger.cash), str(equity))
        cash_diff = abs(sleeves_cash - account.cash)
        equity_diff = abs(sleeves_equity - account.equity)
        journal(
            ts,
            "reconcile",
            {
                "sleeves_cash": str(sleeves_cash),
                "broker_cash": str(account.cash),
                "sleeves_equity": str(sleeves_equity),
                "broker_equity": str(account.equity),
            },
        )
        if cash_diff > _RECONCILE_TOLERANCE or equity_diff > _RECONCILE_TOLERANCE:
            raise ReconciliationError(
                f"sleeves != broker at {ts}: cash diff {cash_diff}, equity diff {equity_diff}"
            )

    def _decide(
        self,
        ts,
        cycle,
        bars_by_symbol,
        ledgers,
        strategies,
        sizer,
        governor,
        broker,
        order_alloc,
        pending_crosses,
        order_seq,
        db,
        journal,
    ) -> None:
        history = HistoryView(bars_by_symbol, ts)
        marks = {s: history.last_close(s) for s in bars_by_symbol}
        view_marks = {s: m for s, m in marks.items() if m is not None}
        sleeve_views = [
            ledger.snapshot(ts, view_marks) for ledger in ledgers.values()
        ]

        account = broker.get_account()
        ctx_all = RiskContext(
            ts=ts,
            mode="backtest",
            trading_state=TradingState.ACTIVE,
            account=PortfolioView(scope="account", ts=ts, cash=account.cash, equity=account.equity),
            sleeves=tuple(sleeve_views),
        )

        # Gather approvals across ALL sleeves first, then net by symbol.
        all_approved = []
        for sleeve_id, (strategy, params) in strategies.items():
            ledger = ledgers[sleeve_id]
            ctx = StrategyContext(ts, ledger.snapshot(ts, view_marks), history, params)
            proposals = strategy.on_schedule(ctx)
            if not proposals:
                continue
            for proposal in proposals:
                journal(
                    ts,
                    "proposal",
                    {
                        "sleeve": sleeve_id,
                        "strategy": proposal.strategy,
                        "action": proposal.action.model_dump(mode="json"),
                    },
                )
            intents = sizer.size(proposals, ledger, history, ts)
            if not intents:
                continue
            all_approved.extend(governor.review(intents, ctx_all))

        if not all_approved:
            return

        def submit(sleeve_or_plan, symbol, side, qty, notional, limit_price) -> None:
            coid = f"nwt-{cycle}-{symbol.replace('/', '-')}-{next(order_seq)}"
            ticket = OrderTicket(
                client_order_id=coid,
                symbol=symbol,
                side=side,
                qty=qty,
                notional=notional,
                limit_price=limit_price,
                tif="day",
            )
            order_alloc[coid] = sleeve_or_plan
            ack = broker.submit(ticket)
            sleeve_label = (
                sleeve_or_plan if isinstance(sleeve_or_plan, str) else "netted"
            )
            row = {
                "client_order_id": coid,
                "sleeve": sleeve_label,
                "symbol": symbol,
                "side": side.value,
                "qty": str(qty) if qty else None,
                "limit_price": str(limit_price) if limit_price else None,
                "state": ack.state.value,
                "reason": ack.reason,
            }
            journal(ts, "order", row)
            db.record_order(
                self.run_id,
                sleeve_label,
                ts,
                client_order_id=coid,
                symbol=symbol,
                side=side.value,
                qty=str(qty) if qty else None,
                notional=str(notional) if notional else None,
                limit_price=str(limit_price) if limit_price else None,
                state=ack.state.value,
            )

        # Notional (crypto) flow: not netted in v1, submitted per sleeve.
        for approval in all_approved:
            if approval.intent.qty is None:
                submit(
                    approval.intent.sleeve_id,
                    approval.intent.symbol,
                    approval.intent.side,
                    None,
                    approval.approved_notional,
                    approval.intent.limit_price,
                )

        # Qty flow: net per symbol; crosses queue for the next bar's open.
        for plan in build_net_plans(all_approved):
            if plan.crosses:
                pending_crosses.setdefault(plan.symbol, []).append(plan)
                journal(
                    ts,
                    "net_plan",
                    {
                        "symbol": plan.symbol,
                        "crosses": [c.model_dump(mode="json") for c in plan.crosses],
                        "net_side": plan.net_side.value if plan.net_side else None,
                        "net_qty": str(plan.net_qty),
                    },
                )
            for leg in plan.unnetted_legs:
                submit(
                    leg.sleeve_id, plan.symbol, leg.side, leg.qty, None, leg.limit_price
                )
            if plan.net_side is not None and plan.net_qty > 0:
                target = (
                    plan.residual_legs[0].sleeve_id
                    if len(plan.residual_legs) == 1
                    else plan
                )
                submit(
                    target,
                    plan.symbol,
                    plan.net_side,
                    plan.net_qty,
                    None,
                    plan.net_limit,
                )
