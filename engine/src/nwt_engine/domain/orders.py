from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, model_validator

from nwt_contracts import Side


class OrderState(StrEnum):
    INTENT = "intent"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    ACKED = "acked"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


# Legal transitions; anything else is a bug that must raise, not warn.
_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.INTENT: frozenset({OrderState.APPROVED, OrderState.REJECTED}),
    OrderState.APPROVED: frozenset({OrderState.SUBMITTED, OrderState.REJECTED}),
    OrderState.SUBMITTED: frozenset(
        {OrderState.ACKED, OrderState.REJECTED, OrderState.CANCELED}
    ),
    OrderState.ACKED: frozenset(
        {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED}
    ),
    OrderState.PARTIAL: frozenset(
        {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED}
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}


class IllegalOrderTransition(RuntimeError):
    pass


def assert_transition(current: OrderState, new: OrderState) -> None:
    if new not in _TRANSITIONS[current]:
        raise IllegalOrderTransition(f"{current} -> {new}")


class OrderTicket(BaseModel, frozen=True):
    client_order_id: str
    symbol: str
    side: Side
    qty: Decimal | None = None
    notional: Decimal | None = None
    limit_price: Decimal | None = None   # None = notional market flow (crypto/DI only)
    # `day` for equities (bounds unattended surface: nothing survives the close).
    # Crypto trades 24/7 and Alpaca rejects `day` for it, so crypto uses gtc.
    tif: Literal["day", "ioc", "gtc"] = "day"

    @model_validator(mode="after")
    def _qty_xor_notional(self) -> "OrderTicket":
        if (self.qty is None) == (self.notional is None):
            raise ValueError("exactly one of qty or notional must be set")
        return self


class Fill(BaseModel, frozen=True):
    fill_id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    ts: datetime
    fees: Decimal = Decimal("0")
    source: Literal["broker", "sim", "internal_cross"] = "sim"
