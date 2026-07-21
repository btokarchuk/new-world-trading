"""Portfolio exposure caps: symbol, gross, position count, crypto sleeve,
and sleeve budget. Only buy-side additions create exposure; reductions flow.

Positions are marked at the quote reference, falling back to avg_cost when no
quote is loaded. OrderRef carries no sleeve attribution, so every open buy is
charged against each sleeve's budget: over-blocking is acceptable here,
under-blocking is not.
"""

from decimal import ROUND_FLOOR, Decimal
from typing import ClassVar

from nwt_contracts import AssetClass, OrderIntent, OrderRef, PositionView, Side

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode
from .base import CheckResult, allow, clamp, reject


class ExposureCheck:
    name: ClassVar[str] = "exposure"

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult:
        if intent.side is Side.SELL or intent.reduces_position:
            return allow(self.name)
        if intent.qty is not None and intent.limit_price is not None:
            added = intent.qty * intent.limit_price
        else:
            added = intent.notional
        if added is None:
            return allow(self.name)  # unpriceable qty intent; sizing checks gate it

        def mark(p: PositionView) -> Decimal:
            q = ctx.quotes.get(p.symbol)
            return p.qty * (q.reference if q is not None else p.avg_cost)

        def order_value(o: OrderRef) -> Decimal:
            if o.qty is not None and o.limit_price is not None:
                return o.qty * o.limit_price
            return o.notional if o.notional is not None else Decimal("0")

        limits = cfg.exposure
        clamp_candidates: list[tuple[Decimal, ReasonCode, str]] = []

        def apply_cap(
            current: Decimal, cap: Decimal, reason: ReasonCode, what: str
        ) -> CheckResult | None:
            """Reject, record a clamp candidate, or None when the cap passes."""
            if current + added <= cap:
                return None
            headroom = cap - current
            detail = f"{what}: {current} + {added} > {cap}"
            if intent.qty is not None and intent.limit_price is not None:
                fit = (headroom / intent.limit_price).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            else:
                fit = headroom
            if fit > 0:
                clamp_candidates.append((fit, reason, detail))
                return None
            return reject(self.name, reason, detail)

        account = ctx.base.account
        open_buys = [o for o in ctx.open_orders if o.side is Side.BUY]

        symbol_current = sum(
            (mark(p) for p in account.positions if p.symbol == intent.symbol),
            Decimal("0"),
        ) + sum(
            (order_value(o) for o in open_buys if o.symbol == intent.symbol),
            Decimal("0"),
        )
        result = apply_cap(
            symbol_current,
            limits.max_symbol_notional_usd,
            ReasonCode.SYMBOL_EXPOSURE_CAP,
            f"symbol {intent.symbol}",
        )
        if result is not None:
            return result

        gross_current = sum((mark(p) for p in account.positions), Decimal("0")) + sum(
            (order_value(o) for o in open_buys), Decimal("0")
        )
        result = apply_cap(
            gross_current,
            limits.max_gross_notional_usd,
            ReasonCode.GROSS_EXPOSURE_CAP,
            "gross",
        )
        if result is not None:
            return result

        held_symbols = {p.symbol for p in account.positions if p.qty != 0}
        if (
            intent.symbol not in held_symbols
            and len(held_symbols) >= limits.max_position_count
        ):
            return reject(
                self.name,
                ReasonCode.POSITION_COUNT_CAP,
                f"{len(held_symbols)} positions held, cap {limits.max_position_count}",
            )

        sleeve_view = ctx.sleeve(intent.sleeve_id)
        sleeve_value = (
            sum((mark(p) for p in sleeve_view.positions), Decimal("0"))
            if sleeve_view is not None
            else Decimal("0")
        )
        if intent.asset_class is AssetClass.CRYPTO:
            result = apply_cap(
                sleeve_value,
                limits.crypto_sleeve_max_usd,
                ReasonCode.CRYPTO_SLEEVE_CAP,
                "crypto sleeve",
            )
            if result is not None:
                return result

        budget = cfg.sleeves.get(intent.sleeve_id)
        if budget is None:
            return reject(
                self.name, ReasonCode.SLEEVE_BUDGET_EXCEEDED, "no budget configured"
            )
        sleeve_current = sleeve_value + sum(
            (order_value(o) for o in open_buys), Decimal("0")
        )
        result = apply_cap(
            sleeve_current,
            budget.budget_usd,
            ReasonCode.SLEEVE_BUDGET_EXCEEDED,
            f"sleeve {intent.sleeve_id}",
        )
        if result is not None:
            return result

        if clamp_candidates:
            fit, reason, detail = min(clamp_candidates, key=lambda c: c[0])
            if intent.qty is not None:
                return clamp(self.name, reason, detail, qty=fit)
            return clamp(self.name, reason, detail, notional=fit)
        return allow(self.name)
