from decimal import Decimal

from nwt_engine.domain import Proposal, TargetWeight

from ..base import BaseStrategy, StrategyContext, StrategyParams
from ..registry import register


class TrendMAParams(StrategyParams):
    symbols: list[str]
    ma: int = 200
    band_bps: int = 100
    check_weekday: int = 4


@register("trend_ma")
class TrendMA(BaseStrategy):
    """Per-symbol moving-average trend filter with a hysteresis band.

    Fixed 1/N allocation per symbol; enter above MA*(1+band), exit below
    MA*(1-band), do nothing inside the band. Decides once a week.
    """

    params_model = TrendMAParams

    def on_schedule(self, ctx: StrategyContext) -> list[Proposal]:
        params: TrendMAParams = ctx.params  # type: ignore[assignment]
        if ctx.now.weekday() != params.check_weekday:
            return []

        band = Decimal(params.band_bps) / Decimal(10000)
        weight = Decimal(1) / Decimal(len(params.symbols))
        held = {p.symbol for p in ctx.sleeve.positions if p.qty > 0}
        proposals: list[Proposal] = []
        for symbol in params.symbols:
            closes = [b.close for b in ctx.history.bars(symbol, params.ma)]
            if len(closes) < params.ma:
                continue
            close = closes[-1]
            ma = sum(closes) / Decimal(params.ma)
            if symbol not in held and close > ma * (1 + band):
                target = weight
            elif symbol in held and close < ma * (1 - band):
                target = Decimal("0")
            else:
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
