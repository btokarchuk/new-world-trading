"""Symbol cooldowns: no re-entry while a cooldown latch is running."""

from typing import ClassVar

from nwt_contracts import OrderIntent

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode
from .base import CheckResult, allow, reject


class CooldownCheck:
    name: ClassVar[str] = "cooldown"

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult:
        if intent.reduces_position:
            return allow(self.name)
        for cd in ctx.cooldowns:
            if cd.symbol == intent.symbol and cd.until > ctx.now:
                return reject(
                    self.name,
                    ReasonCode.SYMBOL_COOLDOWN,
                    f"{intent.symbol} cooling down until {cd.until.isoformat()}",
                )
        return allow(self.name)
