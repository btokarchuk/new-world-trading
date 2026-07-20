from decimal import Decimal

from nwt_engine.domain import Proposal, TargetWeight

from ..base import BaseStrategy, StrategyContext, StrategyParams
from ..registry import register


class BuyHoldParams(StrategyParams):
    symbol: str = "SPY"


@register("buyhold")
class BuyHold(BaseStrategy):
    """The control sleeve: buy on the first decision, then never touch it.

    Drift is allowed by design — this is the honest benchmark every other
    sleeve must beat, with identical cost treatment.
    """

    params_model = BuyHoldParams

    def __init__(self) -> None:
        self._entered = False

    def on_schedule(self, ctx: StrategyContext) -> list[Proposal]:
        params: BuyHoldParams = ctx.params  # type: ignore[assignment]
        if self._entered:
            return []
        if ctx.history.last_close(params.symbol) is None:
            return []
        self._entered = True
        return [
            Proposal(
                sleeve_id=ctx.sleeve.scope,
                strategy=self.name,
                as_of=ctx.now,
                action=TargetWeight(symbol=params.symbol, weight=Decimal("1")),
            )
        ]
