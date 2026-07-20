"""Engine events with a total ordering: (ts, priority, seq).

Priority fixes same-timestamp ordering so replays are deterministic regardless
of arrival jitter; seq is a monotonic tiebreaker assigned at enqueue time.
"""

import heapq
import itertools
from dataclasses import dataclass, field
from datetime import datetime

from nwt_engine.domain import Bar, Fill

# Lower number processes first at an identical timestamp: session changes gate
# bars; bars precede the fills they trigger in sim; schedule (decision) events
# run only after all market/fill state at that instant is applied.
PRIORITY_SESSION = 0
PRIORITY_BAR = 1
PRIORITY_FILL = 2
PRIORITY_ORDER_STATUS = 3
PRIORITY_SCHEDULE = 4


@dataclass(frozen=True)
class BarEvent:
    ts: datetime
    bar: Bar
    priority: int = field(default=PRIORITY_BAR)


@dataclass(frozen=True)
class FillEvent:
    ts: datetime
    fill: Fill
    priority: int = field(default=PRIORITY_FILL)


@dataclass(frozen=True)
class ScheduleEvent:
    """A decision point: strategies are consulted after state at ts is applied."""

    ts: datetime
    label: str
    priority: int = field(default=PRIORITY_SCHEDULE)


Event = BarEvent | FillEvent | ScheduleEvent


class EventQueue:
    def __init__(self) -> None:
        self._heap: list[tuple[datetime, int, int, Event]] = []
        self._seq = itertools.count()

    def push(self, event: Event) -> None:
        heapq.heappush(self._heap, (event.ts, event.priority, next(self._seq), event))

    def pop(self) -> Event:
        return heapq.heappop(self._heap)[3]

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)
