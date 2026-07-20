"""Vectorized reference implementations of the classical strategies.

Independent re-implementations used by the parity harness: same decision rules
as the event-driven engine classes, expressed over whole price frames at once.
Research-side statistics code, so float arithmetic is acceptable here.
"""

import pandas as pd


def momentum_target_weights(
    closes: pd.DataFrame,
    n: int,
    lookback: int,
    skip: int,
    abs_filter: bool,
    rebalance_month_change: bool = True,
) -> pd.DataFrame:
    """Target weights for cross-sectional momentum rotation.

    Momentum at a decision date is close[-1-skip] / close[-lookback] - 1 over
    the bars visible at that date (raw closes). Top-n symbols get 1/n each;
    with abs_filter, a selected symbol with momentum <= 0 gets 0 instead (that
    slice stays in cash). Weights change only on month-change dates (the first
    row counts as one) unless rebalance_month_change is False, in which case
    every row rebalances. A rebalance where no symbol has full lookback
    history keeps the previous weights; symbols without history rank as absent.
    """
    if not 0 <= skip < lookback:
        raise ValueError("require 0 <= skip < lookback")
    if n < 1:
        raise ValueError("n must be >= 1")
    closes = closes.sort_index()
    # Visible-bar indexing: close[-1-skip] == shift(skip), close[-lookback] == shift(lookback - 1)
    mom = closes.shift(skip) / closes.shift(lookback - 1) - 1.0

    weights = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    current = pd.Series(0.0, index=closes.columns)
    prev_month: tuple[int, int] | None = None
    for ts in closes.index:
        month = (ts.year, ts.month)
        is_rebalance = not rebalance_month_change or month != prev_month
        prev_month = month
        if is_rebalance:
            valid = mom.loc[ts].dropna()
            if not valid.empty:
                # ties break by symbol, matching the engine's (-momentum, symbol) sort
                ranked = valid.sort_index().sort_values(ascending=False, kind="stable")
                new = pd.Series(0.0, index=closes.columns)
                for sym in ranked.index[:n]:
                    if abs_filter and valid[sym] <= 0:
                        continue
                    new[sym] = 1.0 / n
                current = new
        weights.loc[ts] = current
    return weights


def trend_target_weights(
    closes: pd.DataFrame,
    ma: int,
    band_bps: int,
    check_weekday: int,
) -> pd.DataFrame:
    """Target weights for the MA trend filter with a hysteresis band.

    On rows falling on check_weekday, each symbol flips in when close exceeds
    the ma-bar simple moving average (current bar inclusive) by band_bps, and
    flips out when close falls below it by band_bps; inside the band the prior
    state holds. Every in symbol gets the fixed 1/len(columns) allocation.
    Initial state is out; rows before the MA has ma bars leave state untouched.
    """
    if ma < 1:
        raise ValueError("ma must be >= 1")
    closes = closes.sort_index()
    ma_ = closes.rolling(ma).mean()
    band = band_bps / 10000.0
    per = 1.0 / len(closes.columns)

    weights = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    state = {sym: False for sym in closes.columns}
    for ts in closes.index:
        if ts.weekday() == check_weekday:
            for sym in closes.columns:
                m = ma_.at[ts, sym]
                if pd.isna(m):
                    continue
                c = closes.at[ts, sym]
                if c > m * (1.0 + band):
                    state[sym] = True
                elif c < m * (1.0 - band):
                    state[sym] = False
        weights.loc[ts] = [per if state[sym] else 0.0 for sym in closes.columns]
    return weights
