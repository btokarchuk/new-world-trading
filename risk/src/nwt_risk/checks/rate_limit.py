"""Order-rate throttles. Protective (stop/exit) intents bypass the per-day
budgets — a cascade of stops must never be throttled into holding losers —
but still obey the per-minute limits, which exist to catch runaway loops."""

from typing import ClassVar

from nwt_contracts import OrderIntent

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode
from .base import CheckResult, allow, reject

MINUTE_S = 60
DAY_S = 86400


class RateLimitCheck:
    name: ClassVar[str] = "rate_limit"

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult:
        def count(window_s: int, symbol: str | None = None) -> int:
            return sum(
                1
                for ro in ctx.recent_orders
                if (symbol is None or ro.symbol == symbol)
                and (ctx.now - ro.ts).total_seconds() < window_s
            )

        if count(MINUTE_S) >= cfg.rate.global_per_min:
            return reject(
                self.name,
                ReasonCode.RATE_GLOBAL_MIN,
                f">= {cfg.rate.global_per_min} orders in 60s",
            )
        if count(MINUTE_S, intent.symbol) >= cfg.rate.per_symbol_per_min:
            return reject(
                self.name,
                ReasonCode.RATE_SYMBOL_MIN,
                f">= {cfg.rate.per_symbol_per_min} {intent.symbol} orders in 60s",
            )
        if not intent.is_protective:
            if count(DAY_S) >= cfg.rate.global_per_day:
                return reject(
                    self.name,
                    ReasonCode.RATE_GLOBAL_DAY,
                    f">= {cfg.rate.global_per_day} orders in 24h",
                )
            if count(DAY_S, intent.symbol) >= cfg.rate.per_symbol_per_day:
                return reject(
                    self.name,
                    ReasonCode.RATE_SYMBOL_DAY,
                    f">= {cfg.rate.per_symbol_per_day} {intent.symbol} orders in 24h",
                )
        return allow(self.name)
