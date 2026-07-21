"""RiskGovernor: the sole gateway between strategy intents and any real broker.

Aggregation rules:
- ALL checks always run (full reason set in the audit log — no short-circuit).
- Any reject => reject. Otherwise clamps compose: the smallest clamped qty/
  notional wins. The governor may size DOWN, never up (enforced again by
  ApprovedOrder's own validator).
- TradingState is enforced both as a check and structurally here: in HALTED
  nothing is approvable; in REDUCING only reduces_position intents are.

The governor never fabricates context: it evaluates exactly the
GovernorContext it is handed, and its verdicts carry the config hash they
were made under.
"""

import itertools
from typing import Callable

from pydantic import BaseModel

from nwt_contracts import ApprovedOrder, OrderIntent, TradingState

from .checks.base import CheckResult, PreTradeCheck
from .config import RiskConfig
from .context import GovernorContext
from .reasons import ReasonCode


class Verdict(BaseModel, frozen=True):
    intent_id: str
    decision: str                    # "allow" | "clamp" | "reject"
    results: tuple[CheckResult, ...]
    config_hash: str

    @property
    def reject_reasons(self) -> list[ReasonCode]:
        return [r.reason for r in self.results if r.decision == "reject" and r.reason]


class ReviewOutcome(BaseModel, frozen=True):
    approved: tuple[ApprovedOrder, ...]
    verdicts: tuple[Verdict, ...]


class RiskGovernor:
    def __init__(
        self,
        checks: list[PreTradeCheck],
        config: RiskConfig,
        state_fn: Callable[[], TradingState],
        audit: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._checks = checks
        self._cfg = config
        self._state_fn = state_fn
        self._audit = audit or (lambda kind, payload: None)
        self._approval_seq = itertools.count(1)

    @property
    def config(self) -> RiskConfig:
        return self._cfg

    def trading_state(self) -> TradingState:
        return self._state_fn()

    def review(self, intents: list[OrderIntent], ctx: GovernorContext) -> ReviewOutcome:
        state = self._state_fn()
        approved: list[ApprovedOrder] = []
        verdicts: list[Verdict] = []

        for intent in intents:
            results = [check.evaluate(intent, ctx, self._cfg) for check in self._checks]

            # Structural state gate — belt and suspenders over the state check.
            if not state.allows(reduces_position=intent.reduces_position):
                results.append(
                    CheckResult(
                        check="structural_state_gate",
                        decision="reject",
                        reason=ReasonCode.STATE_NOT_ACTIVE,
                        detail=f"trading state {state.value}",
                    )
                )

            if any(r.decision == "reject" for r in results):
                decision = "reject"
            elif any(r.decision == "clamp" for r in results):
                decision = "clamp"
            else:
                decision = "allow"

            verdict = Verdict(
                intent_id=intent.intent_id,
                decision=decision,
                results=tuple(results),
                config_hash=self._cfg.config_hash,
            )
            verdicts.append(verdict)
            self._audit(
                "verdict",
                {
                    "intent_id": intent.intent_id,
                    "sleeve": intent.sleeve_id,
                    "symbol": intent.symbol,
                    "side": intent.side.value,
                    "decision": decision,
                    "results": [r.model_dump(mode="json") for r in verdict.results],
                    "config_hash": self._cfg.config_hash,
                },
            )
            if decision == "reject":
                continue

            qty = intent.qty
            notional = intent.notional
            if decision == "clamp":
                qty_clamps = [r.clamped_qty for r in results if r.clamped_qty is not None]
                notional_clamps = [
                    r.clamped_notional for r in results if r.clamped_notional is not None
                ]
                if qty is not None and qty_clamps:
                    qty = min(qty_clamps)
                if notional is not None and notional_clamps:
                    notional = min(notional_clamps)
                if (qty is not None and qty <= 0) or (notional is not None and notional <= 0):
                    continue  # clamped to nothing == reject with the clamp reasons logged

            approved.append(
                ApprovedOrder(
                    intent=intent,
                    approved_qty=qty,
                    approved_notional=notional,
                    approval_id=f"gov-{next(self._approval_seq)}",
                    approved_at=ctx.now,
                )
            )
        return ReviewOutcome(approved=tuple(approved), verdicts=tuple(verdicts))
