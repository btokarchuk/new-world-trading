"""Market calendars.

XNYS sessions come from exchange-calendars (half-days handled upstream);
crypto is a trivial 24/7 calendar with UTC-midnight daily bars.
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd


class MarketCalendar(ABC):
    name: str

    @abstractmethod
    def daily_closes(self, start: datetime, end: datetime) -> list[datetime]:
        """UTC timestamps of session closes within [start, end]."""

    @abstractmethod
    def is_session(self, ts: datetime) -> bool: ...


@lru_cache(maxsize=4)
def _xnys() -> xcals.ExchangeCalendar:
    return xcals.get_calendar("XNYS")


class EquityCalendar(MarketCalendar):
    name = "XNYS"

    def daily_closes(self, start: datetime, end: datetime) -> list[datetime]:
        cal = _xnys()
        sessions = cal.sessions_in_range(
            pd.Timestamp(start).tz_convert(None).normalize(),
            pd.Timestamp(end).tz_convert(None).normalize(),
        )
        if not len(sessions):
            return []
        closes = cal.schedule.loc[sessions, "close"]
        return [c.to_pydatetime().astimezone(UTC) for c in closes]

    def is_session(self, ts: datetime) -> bool:
        return bool(_xnys().is_session(pd.Timestamp(ts).tz_convert(None).normalize()))


class CryptoCalendar(MarketCalendar):
    name = "24_7"

    def daily_closes(self, start: datetime, end: datetime) -> list[datetime]:
        # Daily bars close at UTC midnight (close of day D stamped at D+1 00:00).
        first = datetime(start.year, start.month, start.day, tzinfo=UTC) + timedelta(days=1)
        closes = []
        ts = first
        while ts <= end:
            closes.append(ts)
            ts += timedelta(days=1)
        return closes

    def is_session(self, ts: datetime) -> bool:
        return True


def get_market_calendar(name: str) -> MarketCalendar:
    if name == "XNYS":
        return EquityCalendar()
    if name == "24_7":
        return CryptoCalendar()
    raise ValueError(f"unknown calendar: {name}")
