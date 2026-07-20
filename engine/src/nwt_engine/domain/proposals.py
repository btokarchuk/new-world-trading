from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, model_validator

from nwt_contracts import Side


class TargetWeight(BaseModel, frozen=True):
    """Desired fraction of sleeve equity in a symbol. Long-only v1: [0, 1]."""

    symbol: str
    weight: Decimal

    @model_validator(mode="after")
    def _range(self) -> "TargetWeight":
        if not Decimal("0") <= self.weight <= Decimal("1"):
            raise ValueError("long-only v1: weight must be within [0, 1]")
        return self


class Trade(BaseModel, frozen=True):
    """Explicit trade request (used by mean-reversion style strategies)."""

    symbol: str
    side: Side
    qty: Decimal | None = None
    notional: Decimal | None = None
    limit_hint: Decimal | None = None


class Proposal(BaseModel, frozen=True):
    sleeve_id: str
    strategy: str
    as_of: datetime
    action: TargetWeight | Trade
    confidence: float | None = None   # LLM sleeve only
    rationale: str | None = None      # audit trail
