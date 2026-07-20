import math
from decimal import ROUND_DOWN, Decimal

from nwt_contracts import Side

from nwt_engine.domain import Proposal, Trade

from ..base import BaseStrategy, StrategyContext, StrategyParams
from ..registry import register


class MeanRevZScoreParams(StrategyParams):
    symbols: list[str]
    lookback: int = 20
    entry_z: float = -2.0
    exit_z: float = 0.0
    max_positions: int = 5
    time_stop_days: int = 10


@register("meanrev_zscore")
class MeanRevZScore(BaseStrategy):
    """Short-term mean reversion: limit buys at z <= entry_z, exit at
    z >= exit_z or after time_stop_days decisions.

    Held-ness always comes from the sleeve ledger; only entry age is
    instance state (keyed by symbol, cleared on exit proposal).
    """

    params_model = MeanRevZScoreParams

    def __init__(self) -> None:
        self._decisions = 0
        self._entered_at: dict[str, int] = {}

    def on_schedule(self, ctx: StrategyContext) -> list[Proposal]:
        params: MeanRevZScoreParams = ctx.params  # type: ignore[assignment]
        self._decisions += 1
        held = {p.symbol: p.qty for p in ctx.sleeve.positions if p.qty > 0}
        # Positions acquired without recorded age (e.g. restored state) start
        # their time-stop clock now.
        for symbol in held:
            self._entered_at.setdefault(symbol, self._decisions)
        open_slots = params.max_positions - len(held)

        proposals: list[Proposal] = []
        for symbol in sorted(params.symbols):
            closes = [float(b.close) for b in ctx.history.bars(symbol, params.lookback)]
            if len(closes) < params.lookback:
                continue
            mean = sum(closes) / params.lookback
            std = math.sqrt(sum((c - mean) ** 2 for c in closes) / params.lookback)
            if std == 0:
                continue
            z = (closes[-1] - mean) / std
            close = ctx.history.last_close(symbol)
            if close is None or close <= 0:
                continue

            if symbol in held:
                age = self._decisions - self._entered_at[symbol]
                if z >= params.exit_z or age >= params.time_stop_days:
                    self._entered_at.pop(symbol, None)
                    proposals.append(
                        self._trade(ctx, Trade(
                            symbol=symbol, side=Side.SELL,
                            qty=held[symbol], limit_hint=close,
                        ))
                    )
            elif z <= params.entry_z and open_slots > 0:
                qty = (
                    ctx.sleeve.equity / Decimal(params.max_positions) / close
                ).to_integral_value(rounding=ROUND_DOWN)
                if qty <= 0:
                    continue
                open_slots -= 1
                self._entered_at[symbol] = self._decisions
                proposals.append(
                    self._trade(ctx, Trade(
                        symbol=symbol, side=Side.BUY, qty=qty, limit_hint=close,
                    ))
                )
        return proposals

    def _trade(self, ctx: StrategyContext, trade: Trade) -> Proposal:
        return Proposal(
            sleeve_id=ctx.sleeve.scope,
            strategy=self.name,
            as_of=ctx.now,
            action=trade,
        )
