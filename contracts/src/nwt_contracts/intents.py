"""The RiskGovernor seam: what strategies emit, what the governor returns.

Design invariants encoded in these types:
- Market orders are unrepresentable for equities: every OrderIntent carries a
  limit price context via `limit_price` except crypto/DI notional flows, which
  are explicitly marked and separately gated by the governor.
- The governor may size DOWN (clamp), never up: ApprovedOrder.approved_qty must
  not exceed the intent's qty.
- Every intent carries provenance so LLM-originated flow can be held to
  stricter limits and audited end-to-end via `intent_id`.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, model_validator

from .state import TradingState


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"


Provenance = Literal["classical", "llm", "model", "operator", "control"]


class OrderIntent(BaseModel, frozen=True):
    intent_id: str
    sleeve_id: str
    strategy: str
    symbol: str
    asset_class: AssetClass
    side: Side
    qty: Decimal | None = None            # whole shares for equities
    notional: Decimal | None = None       # crypto / (later) DI fractional flows only
    limit_price: Decimal | None = None    # None only where notional flow is allowed
    as_of: datetime                       # data timestamp the decision was based on
    created_at: datetime
    reduces_position: bool = False        # lets governor pass exits in REDUCING
    is_protective: bool = False           # stop/exit orders; relaxed throttling
    stop_price: Decimal | None = None     # protective intents only: resting stop trigger
    provenance: Provenance = "classical"

    @model_validator(mode="after")
    def _qty_xor_notional(self) -> "OrderIntent":
        if (self.qty is None) == (self.notional is None):
            raise ValueError("exactly one of qty or notional must be set")
        if self.qty is not None and self.qty <= 0:
            raise ValueError("qty must be positive")
        if self.notional is not None and self.notional <= 0:
            raise ValueError("notional must be positive")
        # Protective intents are the narrow exception to "every equity order is
        # a limit": they carry stop_price INSTEAD of limit_price, and the shape
        # is locked down hard because this is the seam everything else trusts.
        if self.is_protective:
            if self.side is not Side.SELL:
                raise ValueError("protective intents must be SELL (long-only book)")
            if not self.reduces_position:
                raise ValueError("protective intents must reduce position")
            if self.stop_price is None or self.stop_price <= 0:
                raise ValueError("protective intents require a positive stop_price")
            if self.limit_price is not None:
                raise ValueError(
                    "protective intents are plain stops: stop_price only, no limit"
                    " (a stop-limit that gaps through its limit is not protection)"
                )
        elif self.stop_price is not None:
            raise ValueError("stop_price is reserved for protective intents")
        if self.asset_class in (AssetClass.EQUITY, AssetClass.ETF):
            if self.qty is None:
                raise ValueError("equity/ETF intents must be whole-share qty, not notional")
            if self.qty != self.qty.to_integral_value():
                raise ValueError("equity/ETF qty must be whole shares")
            if self.limit_price is None and not self.is_protective:
                raise ValueError("equity/ETF intents require a limit price (no market orders)")
        return self


class ApprovedOrder(BaseModel, frozen=True):
    intent: OrderIntent
    approved_qty: Decimal | None = None       # None iff notional flow
    approved_notional: Decimal | None = None
    approval_id: str
    approved_at: datetime

    @model_validator(mode="after")
    def _never_size_up(self) -> "ApprovedOrder":
        if self.intent.qty is not None:
            if self.approved_qty is None or self.approved_qty > self.intent.qty:
                raise ValueError("governor may clamp down, never up (qty)")
        if self.intent.notional is not None:
            if self.approved_notional is None or self.approved_notional > self.intent.notional:
                raise ValueError("governor may clamp down, never up (notional)")
        return self

    # Never-widen-stop is structural, not a validator: ApprovedOrder carries NO
    # approved_stop_price field, so approval is pass/fail on the frozen
    # intent's own stop_price and nothing downstream can move a stop at all.
    # If an approved_stop_price is ever added, add the validator with it.


class PositionView(BaseModel, frozen=True):
    symbol: str
    qty: Decimal
    avg_cost: Decimal


class PortfolioView(BaseModel, frozen=True):
    scope: str                       # sleeve_id or "account"
    ts: datetime
    cash: Decimal
    equity: Decimal
    positions: tuple[PositionView, ...] = ()


class OrderRef(BaseModel, frozen=True):
    client_order_id: str
    symbol: str
    side: Side
    qty: Decimal | None = None
    notional: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    is_protective: bool = False           # resting stops are not working entries
    submitted_at: datetime


class SessionInfo(BaseModel, frozen=True):
    calendar: str                    # "XNYS" | "24_7"
    is_open: bool
    next_open: datetime | None = None
    next_close: datetime | None = None


class RiskContext(BaseModel, frozen=True):
    ts: datetime
    mode: Literal["backtest", "paper", "live"]
    trading_state: TradingState
    account: PortfolioView
    sleeves: tuple[PortfolioView, ...] = ()
    open_orders: tuple[OrderRef, ...] = ()
    sessions: tuple[SessionInfo, ...] = ()
    data_staleness_s: dict[str, float] = {}   # symbol -> seconds since last bar/quote
    last_reconcile_age_s: float | None = None
