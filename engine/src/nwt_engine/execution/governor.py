"""GovernorPort: the seam the risk layer (Phase 3) plugs into.

NullGovernor is BACKTEST ONLY — the experiment runner refuses to construct
paper/live modes without a real governor. Even the null enforces the symbol
whitelist and long-only, so no backtest can quietly trade outside its universe.
"""

import itertools
from typing import Protocol

from nwt_contracts import ApprovedOrder, OrderIntent, RiskContext, Side, TradingState

from nwt_engine.domain import Universe


class GovernorPort(Protocol):
    def review(
        self, intents: list[OrderIntent], ctx: RiskContext
    ) -> list[ApprovedOrder]: ...

    def trading_state(self) -> TradingState: ...


class NullGovernor:
    def __init__(self, universe: Universe) -> None:
        self._universe = universe
        self._seq = itertools.count(1)

    def review(self, intents: list[OrderIntent], ctx: RiskContext) -> list[ApprovedOrder]:
        approved: list[ApprovedOrder] = []
        for intent in intents:
            self._universe.get(intent.symbol)  # KeyError = not whitelisted = bug
            if intent.side is Side.SELL and not intent.reduces_position:
                continue  # long-only even in backtest
            approved.append(
                ApprovedOrder(
                    intent=intent,
                    approved_qty=intent.qty,
                    approved_notional=intent.notional,
                    approval_id=f"null-{next(self._seq)}",
                    approved_at=ctx.ts,
                )
            )
        return approved

    def trading_state(self) -> TradingState:
        return TradingState.ACTIVE
