"""SimBroker: event-driven fill simulation with full account emulation.

Fill model (v1, NextOpenFill): an order accepted during bar T fills at bar T+1's
open plus adverse slippage, mirroring the live execution policy (decide on T's
close, submit T+1 09:31 as marketable limit). A buy limit below next open only
fills if the bar traded through it (conservative: at the limit, never better).

The broker maintains its own cash/positions so reconciliation code paths run in
backtest exactly as they do live. Dividends are credited and splits applied from
the corporate-actions table on ex-date (pay-date lag is a Phase 2 refinement).
"""

import itertools
from datetime import datetime
from decimal import Decimal

from nwt_contracts import Side

from nwt_engine.domain import (
    Bar,
    CorporateAction,
    Fill,
    Instrument,
    OrderState,
    OrderTicket,
    Universe,
)

from ..base import AccountState, Broker, BrokerPosition, OrderAck, OrderStatus
from .costs import CostModel


class _OpenOrder:
    __slots__ = ("ticket", "state", "accepted_ts")

    def __init__(self, ticket: OrderTicket, ts: datetime) -> None:
        self.ticket = ticket
        self.state = OrderState.ACKED
        self.accepted_ts = ts


class SimBroker(Broker):
    def __init__(
        self,
        universe: Universe,
        cost_model: CostModel,
        starting_cash: Decimal,
        clock_now: "callable[[], datetime]",
    ) -> None:
        self._universe = universe
        self._costs = cost_model
        self._cash = starting_cash
        self._positions: dict[str, tuple[Decimal, Decimal]] = {}  # symbol -> (qty, avg_cost)
        self._open: dict[str, _OpenOrder] = {}
        self._pending_fills: list[Fill] = []
        self._last_close: dict[str, Decimal] = {}
        self._fill_seq = itertools.count(1)
        self._now = clock_now
        self._corp_actions: dict[str, list[CorporateAction]] = {}

    # -- setup ---------------------------------------------------------------

    def load_corporate_actions(self, actions: list[CorporateAction]) -> None:
        for a in actions:
            self._corp_actions.setdefault(a.symbol, []).append(a)

    # -- Broker interface ----------------------------------------------------

    def submit(self, ticket: OrderTicket) -> OrderAck:
        now = self._now()
        if ticket.client_order_id in self._open:
            # Idempotent accept: same id returns the original ack (Alpaca-like dedupe).
            return OrderAck(
                client_order_id=ticket.client_order_id, state=OrderState.ACKED, ts=now
            )
        if ticket.side is Side.BUY:
            required = self._order_notional_estimate(ticket)
            if required > self._cash:
                return OrderAck(
                    client_order_id=ticket.client_order_id,
                    state=OrderState.REJECTED,
                    ts=now,
                    reason="insufficient buying power",
                )
        else:
            held = self._positions.get(ticket.symbol, (Decimal("0"), Decimal("0")))[0]
            if ticket.qty is not None and ticket.qty > held:
                return OrderAck(
                    client_order_id=ticket.client_order_id,
                    state=OrderState.REJECTED,
                    ts=now,
                    reason="long-only: cannot sell more than held",
                )
        self._open[ticket.client_order_id] = _OpenOrder(ticket, now)
        return OrderAck(client_order_id=ticket.client_order_id, state=OrderState.ACKED, ts=now)

    def cancel(self, client_order_id: str) -> None:
        self._open.pop(client_order_id, None)

    def cancel_all(self) -> None:
        self._open.clear()

    def get_open_orders(self) -> list[OrderStatus]:
        now = self._now()
        return [
            OrderStatus(
                client_order_id=o.ticket.client_order_id,
                state=o.state,
                filled_qty=Decimal("0"),
                ts=now,
            )
            for o in self._open.values()
        ]

    def get_positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(symbol=s, qty=q, avg_cost=c)
            for s, (q, c) in sorted(self._positions.items())
            if q != 0
        ]

    def get_account(self) -> AccountState:
        equity = self._cash + sum(
            q * self._last_close.get(s, c) for s, (q, c) in self._positions.items()
        )
        return AccountState(ts=self._now(), cash=self._cash, equity=equity)

    def drain_events(self) -> list[Fill]:
        fills, self._pending_fills = self._pending_fills, []
        return fills

    # -- simulation hooks (called by the engine loop) ------------------------

    def on_bar(self, bar: Bar) -> None:
        """Process a new bar: apply corp actions at first sight of the ex-date,
        then attempt fills for resting orders at this bar's open."""
        self._apply_corporate_actions(bar)
        self._last_close[bar.symbol] = bar.close
        for coid in list(self._open):
            order = self._open[coid]
            if order.ticket.symbol != bar.symbol:
                continue
            if order.accepted_ts > bar.ts_open:
                continue  # order arrived during/after this bar; fills next bar
            # Boundary note: accepted_ts == ts_open fills at this bar's open.
            # Required for midnight-stamped daily bars (Alpaca convention:
            # ts_close(N) == ts_open(N+1)), where a close-of-N decision IS the
            # open-of-N+1 instant; economically it is still decide-on-N's-data,
            # fill-at-next-session's-open. Session-true stamps are unaffected.
            fill = self._try_fill(order.ticket, bar)
            if fill is not None:
                self._apply_fill(fill)
                self._pending_fills.append(fill)
                del self._open[coid]

    def expire_day_orders(self) -> None:
        """DAY TIF: called by the loop at session close."""
        self._open = {
            coid: o for coid, o in self._open.items() if o.ticket.tif != "day"
        }

    # -- internals -----------------------------------------------------------

    def _instrument(self, symbol: str) -> Instrument:
        return self._universe.get(symbol)

    def _order_notional_estimate(self, ticket: OrderTicket) -> Decimal:
        if ticket.notional is not None:
            return ticket.notional
        assert ticket.qty is not None
        ref = ticket.limit_price or self._last_close.get(ticket.symbol, Decimal("0"))
        return ticket.qty * ref

    def _try_fill(self, ticket: OrderTicket, bar: Bar) -> Fill | None:
        inst = self._instrument(ticket.symbol)
        if ticket.notional is not None:
            # Notional market flow (crypto/DI): fill at open with slippage.
            price = self._costs.slip_price(inst.asset_class, ticket.side, bar.open)
            qty = (ticket.notional / price).quantize(Decimal("0.000001"))
        else:
            assert ticket.qty is not None
            qty = ticket.qty
            limit = ticket.limit_price
            if limit is None:
                price = self._costs.slip_price(inst.asset_class, ticket.side, bar.open)
            elif ticket.side is Side.BUY:
                if bar.open <= limit:
                    price = self._costs.slip_price(inst.asset_class, Side.BUY, bar.open)
                    price = min(price, limit)  # marketable limit never fills above limit
                elif bar.low <= limit:
                    price = limit  # conservative: at the limit, never better
                else:
                    return None
            else:
                if bar.open >= limit:
                    price = self._costs.slip_price(inst.asset_class, Side.SELL, bar.open)
                    price = max(price, limit)
                elif bar.high >= limit:
                    price = limit
                else:
                    return None
        fees = self._costs.fees(inst.asset_class, qty, price)
        return Fill(
            fill_id=f"simfill-{next(self._fill_seq)}",
            client_order_id=ticket.client_order_id,
            symbol=ticket.symbol,
            side=ticket.side,
            qty=qty,
            price=price.quantize(Decimal("0.0001")),
            ts=bar.ts_open,
            fees=fees,
            source="sim",
        )

    def _apply_fill(self, fill: Fill) -> None:
        qty, avg = self._positions.get(fill.symbol, (Decimal("0"), Decimal("0")))
        if fill.side is Side.BUY:
            new_qty = qty + fill.qty
            new_avg = ((qty * avg) + (fill.qty * fill.price)) / new_qty
            self._positions[fill.symbol] = (new_qty, new_avg)
            self._cash -= fill.qty * fill.price + fill.fees
        else:
            self._positions[fill.symbol] = (qty - fill.qty, avg)
            self._cash += fill.qty * fill.price - fill.fees

    def _apply_corporate_actions(self, bar: Bar) -> None:
        actions = self._corp_actions.get(bar.symbol)
        if not actions:
            return
        remaining: list[CorporateAction] = []
        for action in actions:
            if action.ex_date <= bar.ts_open:
                qty, avg = self._positions.get(bar.symbol, (Decimal("0"), Decimal("0")))
                if action.kind == "dividend" and qty > 0:
                    self._cash += qty * action.cash
                elif action.kind == "split" and qty != 0:
                    self._positions[bar.symbol] = (qty * action.ratio, avg / action.ratio)
            else:
                remaining.append(action)
        self._corp_actions[bar.symbol] = remaining
