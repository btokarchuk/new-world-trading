from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel


class Timeframe(StrEnum):
    D1 = "1d"
    H1 = "1h"


class Bar(BaseModel, frozen=True):
    symbol: str
    timeframe: Timeframe
    ts_open: datetime   # UTC, tz-aware
    ts_close: datetime  # UTC, tz-aware; event time for the engine
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class CorporateAction(BaseModel, frozen=True):
    symbol: str
    ex_date: datetime            # UTC midnight of ex-date
    kind: str                    # "dividend" | "split"
    cash: Decimal = Decimal("0")     # per-share cash for dividends
    ratio: Decimal = Decimal("1")    # new/old share ratio for splits
