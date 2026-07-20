"""Walk-forward splitters over a DatetimeIndex, returned as position slices.

A "year" is a fixed 365 days so split boundaries are exact and reproducible;
calendar-precision windows are not needed for research splitting.
"""

from datetime import timedelta

import pandas as pd

_DAYS_PER_YEAR = 365.0


def rolling_splits(
    index: pd.DatetimeIndex,
    train_years: float,
    test_years: float,
    step_years: float | None = None,
) -> list[tuple[slice, slice]]:
    """Rolling-origin (train, test) slices; the train window slides forward.

    Windows advance by step_years (default: test_years, which tiles the test
    windows edge to edge). The final test window may be partial; a window with
    no test rows ends the sequence.
    """
    return _splits(index, train_years, test_years, step_years, expanding=False)


def expanding_splits(
    index: pd.DatetimeIndex,
    train_years: float,
    test_years: float,
    step_years: float | None = None,
) -> list[tuple[slice, slice]]:
    """Like rolling_splits, but every train window starts at index[0]."""
    return _splits(index, train_years, test_years, step_years, expanding=True)


def _boundary(index: pd.DatetimeIndex, years_from_origin: float) -> int:
    t = index[0] + timedelta(days=round(years_from_origin * _DAYS_PER_YEAR))
    return int(index.searchsorted(t, side="left"))


def _splits(
    index: pd.DatetimeIndex,
    train_years: float,
    test_years: float,
    step_years: float | None,
    expanding: bool,
) -> list[tuple[slice, slice]]:
    if train_years <= 0 or test_years <= 0:
        raise ValueError("train_years and test_years must be positive")
    step = test_years if step_years is None else step_years
    if step <= 0:
        raise ValueError("step_years must be positive")
    if len(index) == 0:
        return []
    if not index.is_monotonic_increasing:
        raise ValueError("index must be sorted ascending")

    out: list[tuple[slice, slice]] = []
    k = 0
    while True:
        off = k * step
        i0 = 0 if expanding else _boundary(index, off)
        i1 = _boundary(index, off + train_years)
        i2 = _boundary(index, off + train_years + test_years)
        if i1 >= len(index) or i2 <= i1 or i1 <= i0:
            break
        out.append((slice(i0, i1), slice(i1, i2)))
        k += 1
    return out
