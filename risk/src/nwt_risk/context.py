"""GovernorContext: everything a pre-trade check may consult.

Built fresh each cycle by the runtime from ledger truth + market data + the
recent-order log. Checks are pure functions of (intent, context, config) —
no check may reach outside this object."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from nwt_contracts import OrderRef, PortfolioView, RiskContext, Side


class QuoteView(BaseModel, frozen=True):
    symbol: str
    ts: datetime
    last: Decimal              # last trade / bar close
    bid: Decimal | None = None
    ask: Decimal | None = None

    @property
    def reference(self) -> Decimal:
        """NBBO mid when both sides exist, else last."""
        if self.bid is not None and self.ask is not None and self.bid > 0:
            return (self.bid + self.ask) / 2
        return self.last


class RecentOrder(BaseModel, frozen=True):
    ts: datetime
    symbol: str
    side: Side
    sleeve_id: str
    is_entry: bool
    is_protective: bool = False  # re-arms must not trip the duplicate window


class SymbolCooldown(BaseModel, frozen=True):
    symbol: str
    until: datetime


class GovernorContext(BaseModel, frozen=True):
    base: RiskContext                                # seam context (state, views, staleness)
    quotes: dict[str, QuoteView] = {}
    recent_orders: tuple[RecentOrder, ...] = ()      # rolling 24h
    open_orders: tuple[OrderRef, ...] = ()
    cooldowns: tuple[SymbolCooldown, ...] = ()
    adv_by_symbol: dict[str, Decimal] = {}           # 20d average daily volume (shares)
    clock_skew_s: float = 0.0

    @property
    def now(self) -> datetime:
        return self.base.ts

    def sleeve(self, sleeve_id: str) -> PortfolioView | None:
        for view in self.base.sleeves:
            if view.scope == sleeve_id:
                return view
        return None
