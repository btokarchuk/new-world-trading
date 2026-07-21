"""Universe eligibility: allowlist membership, penny-stock floor, ADV impact cap.

The allowlist is ctx.adv_by_symbol itself: the runtime only loads ADV for
symbols that passed vetting, so membership in that map IS the vetted tradable
universe — no separate symbol list to drift out of sync.
"""

from decimal import ROUND_FLOOR
from typing import ClassVar

from nwt_contracts import AssetClass, OrderIntent

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode
from .base import CheckResult, allow, clamp, reject


class UniverseCheck:
    name: ClassVar[str] = "universe"

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult:
        adv = ctx.adv_by_symbol.get(intent.symbol)
        if adv is None:
            return reject(
                self.name,
                ReasonCode.NOT_IN_UNIVERSE,
                f"{intent.symbol} not in vetted universe",
            )
        quote = ctx.quotes.get(intent.symbol)
        # Missing quote is StalenessCheck's reject; don't double-report here.
        if (
            intent.asset_class in (AssetClass.EQUITY, AssetClass.ETF)
            and quote is not None
            and quote.reference < cfg.exposure.min_price_usd
        ):
            return reject(
                self.name,
                ReasonCode.PRICE_TOO_LOW,
                f"reference {quote.reference} < {cfg.exposure.min_price_usd}",
            )
        if intent.qty is not None:
            max_qty = cfg.exposure.max_pct_adv * adv
            if intent.qty > max_qty:
                floor_qty = max_qty.to_integral_value(rounding=ROUND_FLOOR)
                detail = f"qty {intent.qty} > {cfg.exposure.max_pct_adv} * ADV {adv}"
                if floor_qty > 0:
                    return clamp(
                        self.name, ReasonCode.ADV_EXCEEDED, detail, qty=floor_qty
                    )
                return reject(self.name, ReasonCode.ADV_EXCEEDED, detail)
        return allow(self.name)
