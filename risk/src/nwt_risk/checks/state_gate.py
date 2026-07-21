"""Trading-state gate: the first check in the catalog."""

from typing import ClassVar

from nwt_contracts import OrderIntent

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode
from .base import CheckResult, allow, reject


class StateGateCheck:
    name: ClassVar[str] = "state_gate"

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult:
        state = ctx.base.trading_state
        if not state.allows(reduces_position=intent.reduces_position):
            return reject(
                self.name,
                ReasonCode.STATE_NOT_ACTIVE,
                f"trading state {state.value}",
            )
        return allow(self.name)
