from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nwt_contracts import Side
from nwt_engine.sleeves import LedgerEntry, LedgerInvariantError, SleeveLedger

TS = datetime(2026, 1, 5, tzinfo=UTC)


def _fill(side: Side, qty: str, price: str, fees: str = "0") -> LedgerEntry:
    return LedgerEntry(
        kind="fill",
        ts=TS,
        symbol="SPY",
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        fees=Decimal(fees),
    )


def test_buy_sell_roundtrip_conserves_cash():
    ledger = SleeveLedger("s1", Decimal("1000"))
    ledger.apply(_fill(Side.BUY, "2", "100", "1"))
    assert ledger.cash == Decimal("799")
    assert ledger.position_qty("SPY") == 2
    ledger.apply(_fill(Side.SELL, "2", "110", "1"))
    assert ledger.cash == Decimal("1018")
    assert ledger.position_qty("SPY") == 0
    assert ledger.equity({}) == ledger.cash


def test_cannot_sell_more_than_held():
    ledger = SleeveLedger("s1", Decimal("1000"))
    ledger.apply(_fill(Side.BUY, "1", "100"))
    with pytest.raises(LedgerInvariantError):
        ledger.apply(_fill(Side.SELL, "2", "100"))


def test_dividend_and_split():
    ledger = SleeveLedger("s1", Decimal("0"))
    ledger.apply(_fill(Side.BUY, "4", "0"))  # free shares for arithmetic clarity
    ledger.apply(LedgerEntry(kind="dividend", ts=TS, symbol="SPY", cash=Decimal("2")))
    assert ledger.cash == Decimal("2")
    ledger.apply(LedgerEntry(kind="split", ts=TS, symbol="SPY", ratio=Decimal("4")))
    assert ledger.position_qty("SPY") == 16
    # Equity invariant: cash + qty * mark
    assert ledger.equity({"SPY": Decimal("25")}) == Decimal("402")


def test_equity_requires_marks_for_held_symbols():
    ledger = SleeveLedger("s1", Decimal("100"))
    ledger.apply(_fill(Side.BUY, "1", "50"))
    with pytest.raises(LedgerInvariantError):
        ledger.equity({})
