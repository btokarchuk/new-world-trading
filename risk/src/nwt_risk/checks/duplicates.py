"""Anti-runaway: rolling duplicate window and one-open-entry-per-symbol."""

from typing import ClassVar

from nwt_contracts import OrderIntent, Side

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode
from .base import CheckResult, allow, reject


class DuplicateCheck:
    name: ClassVar[str] = "duplicates"

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult:
        window = cfg.duplicates.rolling_window_s
        for ro in ctx.recent_orders:
            if (ro.symbol, ro.side, ro.sleeve_id) != (
                intent.symbol,
                intent.side,
                intent.sleeve_id,
            ):
                continue
            age = (ctx.now - ro.ts).total_seconds()
            if age < window:
                return reject(
                    self.name,
                    ReasonCode.DUPLICATE_WINDOW,
                    f"same (symbol, side, sleeve) {age:.0f}s ago < {window}s",
                )
        if cfg.duplicates.one_open_entry_per_symbol_per_sleeve and not intent.reduces_position:
            # OrderRef carries no sleeve id, so this is enforced per symbol
            # ACROSS sleeves — stricter than configured, and correct: sleeves
            # net into one account position, so a second open entry on the
            # same symbol doubles real exposure no matter which sleeve sent it.
            for o in ctx.open_orders:
                if o.symbol == intent.symbol and o.side is Side.BUY:
                    return reject(
                        self.name,
                        ReasonCode.OPEN_ENTRY_EXISTS,
                        f"open buy {o.client_order_id} on {intent.symbol}",
                    )
        return allow(self.name)
