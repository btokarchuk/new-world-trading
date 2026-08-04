"""PositionSizer: proposals -> OrderIntents, per sleeve.

Target weights are diffed against the sleeve's current position; equities round
down to whole shares; buys get a marketable-limit buffer above the reference
close (mirrored by SimBroker's next-open fill model)."""

import itertools
from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from nwt_contracts import AssetClass, OrderIntent, Side

from nwt_engine.domain import Proposal, TargetWeight, Trade, Universe
from nwt_engine.sleeves import SleeveLedger
from nwt_engine.strategies import HistoryView

_LIMIT_BUFFER = Decimal("0.005")  # 0.5% marketable-limit aggression
_MIN_TRADE_NOTIONAL = Decimal("25")


def _default_id_factory() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"intent-{next(counter)}"


class PositionSizer:
    """Proposals -> intents.

    SIGNALS come from bars (point-in-time correct, no lookahead). EXECUTION
    PRICES come from live quotes when there are any: pricing a limit off a
    stale bar close guarantees a price-collar rejection after any overnight
    gap, because the governor rightly compares the limit against the live
    market. Observed 2026-08-04 — a weekend gap of ~3% blocked every order.
    Backtests pass no reference prices and keep using bar closes.
    """

    def __init__(
        self,
        universe: Universe,
        id_factory: Callable[[], str] | None = None,
        reference_prices: dict[str, Decimal] | None = None,
    ) -> None:
        self._universe = universe
        self._next_id = id_factory or _default_id_factory()
        self._reference_prices = reference_prices or {}

    def _reference(self, symbol: str, history: HistoryView) -> Decimal | None:
        live = self._reference_prices.get(symbol)
        return live if live is not None else history.last_close(symbol)

    def size(
        self,
        proposals: list[Proposal],
        ledger: SleeveLedger,
        history: HistoryView,
        now: datetime,
        provenance: str = "classical",
    ) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        for proposal in proposals:
            if isinstance(proposal.action, TargetWeight):
                intent = self._from_target_weight(proposal, proposal.action, ledger, history, now)
            else:
                intent = self._from_trade(proposal, proposal.action, history, now)
            if intent is not None:
                intents.append(intent)
        return intents

    def _from_target_weight(
        self,
        proposal: Proposal,
        target: TargetWeight,
        ledger: SleeveLedger,
        history: HistoryView,
        now: datetime,
    ) -> OrderIntent | None:
        inst = self._universe.get(target.symbol)
        ref = self._reference(target.symbol, history)
        if ref is None or ref <= 0:
            return None
        marks = {s: history.last_close(s) or c for s, (q, c) in ledger.positions.items()}
        equity = ledger.equity(marks)
        current = ledger.position_qty(target.symbol)
        # Size against the worst-case fill price (the buy limit), so a full-weight
        # target is always affordable even if the order fills at the limit.
        buy_limit = (ref * (1 + _LIMIT_BUFFER)).quantize(inst.tick_size)
        desired_qty = (target.weight * equity / buy_limit).to_integral_value(
            rounding=ROUND_DOWN
        )
        delta = desired_qty - current
        if delta == 0 or abs(delta) * ref < _MIN_TRADE_NOTIONAL:
            return None
        side = Side.BUY if delta > 0 else Side.SELL
        sell_limit = (ref * (1 - _LIMIT_BUFFER)).quantize(inst.tick_size)
        limit = buy_limit if side is Side.BUY else sell_limit
        return OrderIntent(
            intent_id=self._next_id(),
            sleeve_id=proposal.sleeve_id,
            strategy=proposal.strategy,
            symbol=target.symbol,
            asset_class=inst.asset_class,
            side=side,
            qty=abs(delta),
            limit_price=limit,
            as_of=proposal.as_of,
            created_at=now,
            reduces_position=side is Side.SELL,
            provenance=proposal.strategy == "llm_analyst" and "llm" or "classical",  # type: ignore[arg-type]
        )

    def _from_trade(
        self, proposal: Proposal, trade: Trade, history: HistoryView, now: datetime
    ) -> OrderIntent | None:
        inst = self._universe.get(trade.symbol)
        # A strategy's limit_hint is a signal-space price; the live quote wins
        # for execution when we have one.
        ref = self._reference(trade.symbol, history) or trade.limit_hint
        if ref is None:
            return None
        limit = ref if inst.asset_class is not AssetClass.CRYPTO else None
        return OrderIntent(
            intent_id=self._next_id(),
            sleeve_id=proposal.sleeve_id,
            strategy=proposal.strategy,
            symbol=trade.symbol,
            asset_class=inst.asset_class,
            side=trade.side,
            qty=trade.qty,
            notional=trade.notional,
            limit_price=limit,
            as_of=proposal.as_of,
            created_at=now,
            reduces_position=trade.side is Side.SELL,
        )
