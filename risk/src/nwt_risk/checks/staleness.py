"""Data trust: never act on a quote, ledger, or clock we cannot vouch for."""

from typing import ClassVar

from nwt_contracts import OrderIntent

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode
from .base import CheckResult, allow, reject


class StalenessCheck:
    name: ClassVar[str] = "staleness"

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult:
        quote = ctx.quotes.get(intent.symbol)
        if quote is None:
            return reject(
                self.name, ReasonCode.STALE_QUOTE, f"no quote for {intent.symbol}"
            )
        age = (ctx.now - quote.ts).total_seconds()
        if age > cfg.staleness.max_quote_age_s:
            return reject(
                self.name,
                ReasonCode.STALE_QUOTE,
                f"quote age {age:.1f}s > {cfg.staleness.max_quote_age_s}s",
            )
        reconcile_age = ctx.base.last_reconcile_age_s
        if reconcile_age is None or reconcile_age > cfg.staleness.max_reconcile_age_s:
            return reject(
                self.name,
                ReasonCode.STALE_RECONCILE,
                f"reconcile age {reconcile_age} > {cfg.staleness.max_reconcile_age_s}s",
            )
        if abs(ctx.clock_skew_s) > cfg.staleness.max_clock_skew_s:
            return reject(
                self.name,
                ReasonCode.CLOCK_SKEW,
                f"clock skew {ctx.clock_skew_s}s > {cfg.staleness.max_clock_skew_s}s",
            )
        return allow(self.name)
