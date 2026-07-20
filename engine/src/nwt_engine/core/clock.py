from abc import ABC, abstractmethod
from datetime import UTC, datetime


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime: ...


class SimClock(Clock):
    """Backtest clock: time is whatever the event queue says it is."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock requires tz-aware start")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance_to(self, ts: datetime) -> None:
        if ts < self._now:
            raise RuntimeError(f"clock moved backwards: {self._now} -> {ts}")
        self._now = ts


class WallClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)
