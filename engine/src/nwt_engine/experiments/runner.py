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

from nwt_contracts import PortfolioView, RiskContext, Side, TradingState

from nwt_engine.broker import SimBroker
from nwt_engine.core import BarEvent, EventQueue, ScheduleEvent, SimClock
from nwt_engine.data import HistoricalBarFeed, ParquetStore
from nwt_engine.domain import Bar, CorporateAction, Fill, OrderTicket
from nwt_engine.execution import NullGovernor, PositionSizer
from nwt_engine.sleeves import LedgerEntry, SleeveLedger
from nwt_engine.strategies import BaseStrategy, HistoryView, StrategyContext, get_strategy

from .config import ExperimentConfig
from .db import ResultsDB
from .metrics import compute_metrics


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
        order_sleeve: dict[str, str] = {}   # client_order_id -> sleeve_id
        order_seq = itertools.count(1)
        cycle = 0

        try:
            while queue:
                event = queue.pop()
                clock.advance_to(event.ts)

                if isinstance(event, BarEvent):
                    self._apply_sleeve_corp_actions(
                        event.bar, pending_ca, ledgers, journal
                    )
                    broker.on_bar(event.bar)
                    marks[event.bar.symbol] = event.bar.close
                    journal(
                        event.ts,
                        "bar",
                        {"symbol": event.bar.symbol, "close": str(event.bar.close)},
                    )
                    for fill in broker.drain_events():
                        self._apply_fill(fill, order_sleeve, ledgers, db, journal)

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
                        order_sleeve,
                        order_seq,
                        db,
                        journal,
                    )
                db.commit()
        except Exception:
            db.finish_run(self.run_id, datetime.now(UTC).isoformat(), "failed")
            db.close()
            raise

        results: list[SleeveResult] = []
        for sleeve_id, ledger in ledgers.items():
            rows = db.conn.execute(
                "SELECT equity FROM equity_daily WHERE run_id=? AND sleeve_id=? ORDER BY ts",
                (self.run_id, sleeve_id),
            ).fetchall()
            equity_series = [float(r[0]) for r in rows]
            sleeve_metrics = compute_metrics(equity_series)
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

    def _apply_fill(self, fill: Fill, order_sleeve, ledgers, db, journal) -> None:
        sleeve_id = order_sleeve.get(fill.client_order_id)
        if sleeve_id is None:
            raise ReconciliationError(f"fill for unknown order {fill.client_order_id}")
        ledgers[sleeve_id].apply(
            LedgerEntry(
                kind="fill",
                ts=fill.ts,
                symbol=fill.symbol,
                side=fill.side,
                qty=fill.qty,
                price=fill.price,
                fees=fill.fees,
            )
        )
        payload = {
            "fill_id": fill.fill_id,
            "client_order_id": fill.client_order_id,
            "symbol": fill.symbol,
            "side": fill.side.value,
            "qty": str(fill.qty),
            "price": str(fill.price),
            "fees": str(fill.fees),
            "ts": fill.ts.isoformat(),
        }
        journal(fill.ts, "fill", payload)
        db.record_fill(self.run_id, sleeve_id, **payload)

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
        order_sleeve,
        order_seq,
        db,
        journal,
    ) -> None:
        history = HistoryView(bars_by_symbol, ts)
        marks = {s: history.last_close(s) for s in bars_by_symbol}
        sleeve_views: list[PortfolioView] = []
        for sleeve_id, ledger in ledgers.items():
            view_marks = {
                s: m for s, m in marks.items() if m is not None
            }
            sleeve_views.append(ledger.snapshot(ts, view_marks))

        account = broker.get_account()
        ctx_all = RiskContext(
            ts=ts,
            mode="backtest",
            trading_state=TradingState.ACTIVE,
            account=PortfolioView(scope="account", ts=ts, cash=account.cash, equity=account.equity),
            sleeves=tuple(sleeve_views),
        )

        for sleeve_id, (strategy, params) in strategies.items():
            ledger = ledgers[sleeve_id]
            view_marks = {s: m for s, m in marks.items() if m is not None}
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
            approved = governor.review(intents, ctx_all)
            for approval in approved:
                intent = approval.intent
                coid = f"nwt-{cycle}-{intent.symbol.replace('/', '-')}-{next(order_seq)}"
                ticket = OrderTicket(
                    client_order_id=coid,
                    symbol=intent.symbol,
                    side=intent.side,
                    qty=approval.approved_qty,
                    notional=approval.approved_notional,
                    limit_price=intent.limit_price,
                    tif="day",
                )
                order_sleeve[coid] = sleeve_id
                ack = broker.submit(ticket)
                journal(
                    ts,
                    "order",
                    {
                        "client_order_id": coid,
                        "sleeve": sleeve_id,
                        "symbol": intent.symbol,
                        "side": intent.side.value,
                        "qty": str(approval.approved_qty) if approval.approved_qty else None,
                        "limit_price": str(intent.limit_price) if intent.limit_price else None,
                        "state": ack.state.value,
                        "reason": ack.reason,
                    },
                )
                db.record_order(
                    self.run_id,
                    sleeve_id,
                    ts,
                    client_order_id=coid,
                    symbol=intent.symbol,
                    side=intent.side.value,
                    qty=str(approval.approved_qty) if approval.approved_qty else None,
                    notional=str(approval.approved_notional)
                    if approval.approved_notional
                    else None,
                    limit_price=str(intent.limit_price) if intent.limit_price else None,
                    state=ack.state.value,
                )
