"""Per-order sizing: fat-finger caps, dust floor, per-sleeve order cap.

Multiple caps can bind at once; a check returns a single result, so the most
binding (smallest) clamp wins here — the governor then takes the min across
checks the same way.
"""

from decimal import ROUND_FLOOR, Decimal
from typing import ClassVar

from nwt_contracts import AssetClass, OrderIntent

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode
from .base import CheckResult, allow, clamp, reject


class OrderSizeCheck:
    name: ClassVar[str] = "order_size"

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult:
        # Protective stops are exempt from sizing entirely: a stop covering
        # less than the full lot is not protection, and a $20 residual must
        # still be stoppable. Notional caps exist to bound what an order can
        # BUY; a resting catastrophe SELL bounds loss instead. Price sanity is
        # the collar's protective band, not a size question.
        if intent.is_protective:
            return allow(self.name)
        equity_like = intent.asset_class in (AssetClass.EQUITY, AssetClass.ETF)
        if equity_like and intent.limit_price is None:
            # Belt over the contracts validator: unrepresentable there, blocked here too.
            return reject(
                self.name,
                ReasonCode.MARKET_ORDER_FORBIDDEN,
                "equity intent without limit price",
            )
        sleeve = cfg.sleeves.get(intent.sleeve_id)
        if sleeve is None:
            return reject(
                self.name, ReasonCode.SLEEVE_BUDGET_EXCEEDED, "no budget configured"
            )
        if intent.qty is not None and intent.limit_price is not None:
            value = intent.qty * intent.limit_price
        else:
            value = intent.notional
        if value is not None and value < cfg.order.min_notional_usd:
            return reject(
                self.name,
                ReasonCode.MIN_NOTIONAL,
                f"notional {value} < {cfg.order.min_notional_usd}",
            )

        candidates: list[tuple[Decimal, ReasonCode, str]] = []
        if intent.qty is not None:
            if intent.qty > cfg.order.max_shares:
                candidates.append(
                    (
                        cfg.order.max_shares,
                        ReasonCode.ORDER_SHARES_CAP,
                        f"qty {intent.qty} > {cfg.order.max_shares}",
                    )
                )
            if value is not None and intent.limit_price is not None:
                if value > cfg.order.max_notional_usd:
                    fit = (cfg.order.max_notional_usd / intent.limit_price).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                    detail = f"notional {value} > {cfg.order.max_notional_usd}"
                    if fit <= 0:
                        return reject(self.name, ReasonCode.ORDER_NOTIONAL_CAP, detail)
                    candidates.append((fit, ReasonCode.ORDER_NOTIONAL_CAP, detail))
                if value > sleeve.max_order_usd:
                    fit = (sleeve.max_order_usd / intent.limit_price).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                    detail = f"notional {value} > sleeve max {sleeve.max_order_usd}"
                    if fit <= 0:
                        return reject(self.name, ReasonCode.SLEEVE_ORDER_CAP, detail)
                    candidates.append((fit, ReasonCode.SLEEVE_ORDER_CAP, detail))
            if candidates:
                fit, reason, detail = min(candidates, key=lambda c: c[0])
                return clamp(self.name, reason, detail, qty=fit)
            return allow(self.name)

        # Notional flow (crypto / DI).
        assert value is not None  # xor validator guarantees notional is set here
        if value > cfg.order.max_notional_usd:
            candidates.append(
                (
                    cfg.order.max_notional_usd,
                    ReasonCode.ORDER_NOTIONAL_CAP,
                    f"notional {value} > {cfg.order.max_notional_usd}",
                )
            )
        if value > sleeve.max_order_usd:
            candidates.append(
                (
                    sleeve.max_order_usd,
                    ReasonCode.SLEEVE_ORDER_CAP,
                    f"notional {value} > sleeve max {sleeve.max_order_usd}",
                )
            )
        if candidates:
            fit, reason, detail = min(candidates, key=lambda c: c[0])
            return clamp(self.name, reason, detail, notional=fit)
        return allow(self.name)
