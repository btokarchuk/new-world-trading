"""Long-only enforcement: every sell must reduce a position we actually hold."""

from decimal import Decimal
from typing import ClassVar

from nwt_contracts import OrderIntent, Side

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode
from .base import CheckResult, allow, clamp, reject


class LongOnlyCheck:
    name: ClassVar[str] = "long_only"

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult:
        if intent.side is not Side.SELL:
            return allow(self.name)
        view = ctx.sleeve(intent.sleeve_id)
        if view is None:
            return reject(
                self.name,
                ReasonCode.SHORT_FORBIDDEN,
                f"no sleeve view for {intent.sleeve_id}",
            )
        held = next(
            (p.qty for p in view.positions if p.symbol == intent.symbol),
            Decimal("0"),
        )
        if intent.qty is not None:
            if intent.qty > held:
                detail = f"sell {intent.qty} > held {held}"
                if held > 0 and intent.is_protective:
                    return clamp(
                        self.name, ReasonCode.PHANTOM_POSITION, detail, qty=held
                    )
                return reject(self.name, ReasonCode.PHANTOM_POSITION, detail)
        elif held <= 0:
            # Notional sells can't be qty-bounded here, but selling a symbol
            # the sleeve doesn't hold is still a short attempt.
            return reject(
                self.name,
                ReasonCode.PHANTOM_POSITION,
                f"notional sell of unheld {intent.symbol}",
            )
        return allow(self.name)
