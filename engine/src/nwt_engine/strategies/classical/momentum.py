from decimal import Decimal
from typing import Literal

from nwt_engine.domain import Proposal, TargetWeight

from ..base import BaseStrategy, StrategyContext, StrategyParams
from ..registry import register


class MomentumRotationParams(StrategyParams):
    symbols: list[str]
    n: int = 2
    lookback: int = 252
    skip: int = 21
    rebalance: Literal["monthly"] = "monthly"
    abs_filter: bool = True


@register("momentum_rotation")
class MomentumRotation(BaseStrategy):
    """Cross-sectional 12-1 momentum: hold top-N equal weight, rotate monthly.

    Ranks by close[-1-skip] / close[-lookback] - 1 on raw closes; with
    abs_filter, a winner with momentum <= 0 sits in cash instead.
    """

    params_model = MomentumRotationParams

    def __init__(self) -> None:
        self._last_month: tuple[int, int] | None = None

    def on_schedule(self, ctx: StrategyContext) -> list[Proposal]:
        params: MomentumRotationParams = ctx.params  # type: ignore[assignment]
        month = (ctx.now.year, ctx.now.month)
        is_rebalance = month != self._last_month
        self._last_month = month
        if not is_rebalance:
            return []

        momenta: dict[str, Decimal] = {}
        for symbol in params.symbols:
            closes = [b.close for b in ctx.history.bars(symbol, params.lookback)]
            if len(closes) < params.lookback:
                continue  # unrankable: insufficient history
            momenta[symbol] = closes[-1 - params.skip] / closes[-params.lookback] - 1

        ranked = sorted(momenta, key=lambda s: (-momenta[s], s))
        winners = {
            s
            for s in ranked[: params.n]
            if not (params.abs_filter and momenta[s] <= 0)
        }

        held = {p.symbol for p in ctx.sleeve.positions if p.qty > 0}
        weight = Decimal(1) / Decimal(params.n)
        proposals: list[Proposal] = []
        for symbol in sorted(params.symbols):
            target = weight if symbol in winners else Decimal("0")
            current = weight if symbol in held else Decimal("0")
            if target == current:
                continue
            proposals.append(
                Proposal(
                    sleeve_id=ctx.sleeve.scope,
                    strategy=self.name,
                    as_of=ctx.now,
                    action=TargetWeight(symbol=symbol, weight=target),
                )
            )
        return proposals
