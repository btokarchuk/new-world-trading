"""SleeveLedger: an event-sourced virtual sub-portfolio.

State is a pure fold over typed entries; every entry is journaled before it is
applied. The account-level invariant (sum of sleeves == broker truth) is
asserted by the runner at every reconcile point — the ledger itself only
guarantees its own internal invariant: cash + Σ(qty·mark) == equity, exactly.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from nwt_contracts import PortfolioView, PositionView, Side


class LedgerEntry(BaseModel, frozen=True):
    kind: Literal["fill", "dividend", "capital", "split"]
    ts: datetime
    symbol: str | None = None
    side: Side | None = None
    qty: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")      # dividend/capital credit amount
    ratio: Decimal = Decimal("1")     # split ratio


class LedgerInvariantError(RuntimeError):
    pass


class SleeveLedger:
    def __init__(self, sleeve_id: str, starting_cash: Decimal) -> None:
        self.sleeve_id = sleeve_id
        self.cash = starting_cash
        self.positions: dict[str, tuple[Decimal, Decimal]] = {}  # symbol -> (qty, avg_cost)
        self._applied = 0

    def apply(self, entry: LedgerEntry) -> None:
        if entry.kind == "fill":
            assert entry.symbol is not None and entry.side is not None
            qty, avg = self.positions.get(entry.symbol, (Decimal("0"), Decimal("0")))
            if entry.side is Side.BUY:
                new_qty = qty + entry.qty
                new_avg = ((qty * avg) + (entry.qty * entry.price)) / new_qty
                self.positions[entry.symbol] = (new_qty, new_avg)
                self.cash -= entry.qty * entry.price + entry.fees
            else:
                if entry.qty > qty:
                    raise LedgerInvariantError(
                        f"{self.sleeve_id}: sell {entry.qty} > held {qty} {entry.symbol}"
                    )
                self.positions[entry.symbol] = (qty - entry.qty, avg)
                self.cash += entry.qty * entry.price - entry.fees
        elif entry.kind == "dividend":
            self.cash += entry.cash
        elif entry.kind == "capital":
            self.cash += entry.cash
        elif entry.kind == "split":
            assert entry.symbol is not None
            qty, avg = self.positions.get(entry.symbol, (Decimal("0"), Decimal("0")))
            if qty != 0:
                self.positions[entry.symbol] = (qty * entry.ratio, avg / entry.ratio)
        self._applied += 1

    def position_qty(self, symbol: str) -> Decimal:
        return self.positions.get(symbol, (Decimal("0"), Decimal("0")))[0]

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        total = self.cash
        for symbol, (qty, avg_cost) in self.positions.items():
            if qty == 0:
                continue
            if symbol not in marks:
                raise LedgerInvariantError(f"no mark for held symbol {symbol}")
            total += qty * marks[symbol]
        return total

    def snapshot(self, ts: datetime, marks: dict[str, Decimal]) -> PortfolioView:
        return PortfolioView(
            scope=self.sleeve_id,
            ts=ts,
            cash=self.cash,
            equity=self.equity(marks),
            positions=tuple(
                PositionView(symbol=s, qty=q, avg_cost=c)
                for s, (q, c) in sorted(self.positions.items())
                if q != 0
            ),
        )
