from decimal import Decimal
from typing import ClassVar, Literal, Protocol

from pydantic import BaseModel

from nwt_contracts import OrderIntent

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode


class CheckResult(BaseModel, frozen=True):
    check: str
    decision: Literal["allow", "clamp", "reject"]
    reason: ReasonCode | None = None
    detail: str = ""
    clamped_qty: Decimal | None = None       # set only on clamp of qty intents
    clamped_notional: Decimal | None = None  # set only on clamp of notional intents


def allow(check: str) -> CheckResult:
    return CheckResult(check=check, decision="allow")


def reject(check: str, reason: ReasonCode, detail: str) -> CheckResult:
    return CheckResult(check=check, decision="reject", reason=reason, detail=detail)


def clamp(
    check: str,
    reason: ReasonCode,
    detail: str,
    qty: Decimal | None = None,
    notional: Decimal | None = None,
) -> CheckResult:
    return CheckResult(
        check=check,
        decision="clamp",
        reason=reason,
        detail=detail,
        clamped_qty=qty,
        clamped_notional=notional,
    )


class PreTradeCheck(Protocol):
    name: ClassVar[str]

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult: ...
