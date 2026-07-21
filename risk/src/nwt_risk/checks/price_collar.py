"""Price sanity: reject limits far from the quote reference, and refuse to
trade at all on a quote whose mid and last disagree (one of them is wrong)."""

from typing import ClassVar

from nwt_contracts import AssetClass, OrderIntent

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode
from .base import CheckResult, allow, reject


class PriceCollarCheck:
    name: ClassVar[str] = "price_collar"

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult:
        if intent.qty is None or intent.limit_price is None:
            return allow(self.name)  # notional flow has no limit to collar
        quote = ctx.quotes.get(intent.symbol)
        if quote is None:
            return allow(self.name)  # StalenessCheck rejects missing quotes
        if quote.bid is not None and quote.ask is not None and quote.last > 0:
            mid = (quote.bid + quote.ask) / 2
            divergence_pct = abs(mid - quote.last) / quote.last * 100
            if divergence_pct > cfg.order.suspect_quote_divergence_pct:
                return reject(
                    self.name,
                    ReasonCode.SUSPECT_QUOTE,
                    f"mid {mid} vs last {quote.last} diverge {divergence_pct:.2f}%",
                )
        reference = quote.reference
        if reference <= 0:
            return reject(
                self.name, ReasonCode.SUSPECT_QUOTE, f"non-positive reference {reference}"
            )
        collar = (
            cfg.order.price_collar_pct_crypto
            if intent.asset_class is AssetClass.CRYPTO
            else cfg.order.price_collar_pct_equity
        )
        deviation_pct = abs(intent.limit_price - reference) / reference * 100
        if deviation_pct > collar:
            return reject(
                self.name,
                ReasonCode.PRICE_COLLAR_BREACH,
                f"limit {intent.limit_price} is {deviation_pct:.2f}% from {reference}",
            )
        return allow(self.name)
