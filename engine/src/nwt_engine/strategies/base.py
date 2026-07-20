from abc import ABC
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel

from nwt_contracts import PortfolioView

from nwt_engine.domain import Bar, Proposal


class StrategyParams(BaseModel):
    """Subclass per strategy; validated from experiment config."""


class HistoryView:
    """Bars clamped to ts_close <= now — the lookahead guard.

    Strategies can only ever see completed bars at or before the decision time.
    """

    def __init__(self, bars_by_symbol: dict[str, list[Bar]], now: datetime) -> None:
        self._bars = bars_by_symbol
        self._now = now

    def bars(self, symbol: str, n: int | None = None) -> list[Bar]:
        visible = [b for b in self._bars.get(symbol, []) if b.ts_close <= self._now]
        return visible[-n:] if n is not None else visible

    def last_close(self, symbol: str) -> Decimal | None:
        visible = self.bars(symbol)
        return visible[-1].close if visible else None


class StrategyContext:
    def __init__(
        self,
        now: datetime,
        sleeve: PortfolioView,
        history: HistoryView,
        params: StrategyParams,
    ) -> None:
        self.now = now
        self.sleeve = sleeve
        self.history = history
        self.params = params


class BaseStrategy(ABC):
    name: ClassVar[str]
    params_model: ClassVar[type[StrategyParams]] = StrategyParams
    #: True => backtests refused unless ticker-anonymized (LLM memorization guard)
    contamination_sensitive: ClassVar[bool] = False

    def on_start(self, ctx: StrategyContext) -> None:  # noqa: B027
        pass

    def on_schedule(self, ctx: StrategyContext) -> list[Proposal]:
        return []

    def on_fill(self, ctx: StrategyContext, fill: object) -> None:  # noqa: B027
        pass

    def on_stop(self, ctx: StrategyContext) -> None:  # noqa: B027
        pass
