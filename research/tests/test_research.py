import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from nwt_engine.domain import Bar, Timeframe

from nwt_research.bootstrap import sharpe_ci
from nwt_research.parity import assert_parity, run_engine_strategy, strategy_registered
from nwt_research.vectorized import momentum_target_weights, trend_target_weights
from nwt_research.walkforward import expanding_splits, rolling_splits


def _dt(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 21, 0, tzinfo=UTC)


def _weekday_closes(start: datetime, count: int) -> list[datetime]:
    out: list[datetime] = []
    d = start
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _make_market(
    drifts: dict[str, float], n_bars: int = 420, seed: int = 11
) -> tuple[dict[str, list[Bar]], pd.DataFrame]:
    """Seeded random-walk bars and the matching closes frame from one source."""
    ts_closes = _weekday_closes(_dt(2020, 1, 6), n_bars)
    rng = random.Random(seed)
    closes: dict[str, list[Decimal]] = {}
    for sym in sorted(drifts):
        px = 100.0
        series: list[Decimal] = []
        for _ in ts_closes:
            px = max(1.0, px * (1.0 + rng.gauss(drifts[sym], 0.01)))
            series.append(Decimal(f"{px:.2f}"))
        closes[sym] = series
    bars_by_symbol = {
        sym: [
            Bar(
                symbol=sym,
                timeframe=Timeframe.D1,
                ts_open=ts - timedelta(hours=6, minutes=30),
                ts_close=ts,
                open=c,
                high=c,
                low=c,
                close=c,
                volume=Decimal("1000"),
            )
            for ts, c in zip(ts_closes, closes[sym], strict=True)
        ]
        for sym in closes
    }
    closes_df = pd.DataFrame(
        {sym: [float(c) for c in closes[sym]] for sym in closes},
        index=pd.DatetimeIndex(ts_closes),
    )
    return bars_by_symbol, closes_df


# -- vectorized momentum (hand-verifiable) -----------------------------------

# Weekdays spanning a month change: Jan 26-30 + Feb 2-6, 2026.
_MOM_DATES = pd.DatetimeIndex(
    [_dt(2026, 1, 26 + i) for i in range(5)] + [_dt(2026, 2, 2 + i) for i in range(5)]
)


def test_momentum_hand_verified_rotation():
    closes = pd.DataFrame(
        {
            # A crashes after the Feb rebalance: weights must NOT react mid-month
            "A": [10, 11, 12, 13, 14, 15, 5, 5, 5, 5],
            "B": [20, 19, 18, 17, 16, 15, 14, 13, 12, 11],
            "C": [10, 10.5, 11, 11.5, 12, 12.5, 13, 13, 13, 13],
        },
        index=_MOM_DATES,
        dtype=float,
    )
    w = momentum_target_weights(closes, n=1, lookback=3, skip=1, abs_filter=True)
    # Feb 2 momentum = close[-2]/close[-3] - 1: A 14/13, B 16/17 (<0), C 12/11.5 -> A wins
    expected = pd.DataFrame(0.0, index=_MOM_DATES, columns=["A", "B", "C"])
    expected.loc[_dt(2026, 2, 2) :, "A"] = 1.0
    pd.testing.assert_frame_equal(w, expected)


def test_momentum_abs_filter_zeroes_nonpositive_selection():
    closes = pd.DataFrame(
        {
            "A": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
            "B": [20, 19, 18, 17, 16, 15, 14, 13, 12, 11],
            "C": [30, 29.8, 29.6, 29.4, 29.2, 29.0, 28.8, 28.6, 28.4, 28.2],
        },
        index=_MOM_DATES,
        dtype=float,
    )
    # Feb 2: mom A > 0 > mom C > mom B; top-2 = {A, C}
    reb = _dt(2026, 2, 2)
    w_on = momentum_target_weights(closes, n=2, lookback=3, skip=1, abs_filter=True)
    assert w_on.loc[reb].tolist() == [0.5, 0.0, 0.0]
    w_off = momentum_target_weights(closes, n=2, lookback=3, skip=1, abs_filter=False)
    assert w_off.loc[reb].tolist() == [0.5, 0.0, 0.5]


def test_momentum_rebalance_every_bar_when_disabled():
    closes = pd.DataFrame(
        {
            "A": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
            "B": [20, 19, 18, 17, 16, 15, 14, 13, 12, 11],
            "C": [10, 10.5, 11, 11.5, 12, 12.5, 13, 13, 13, 13],
        },
        index=_MOM_DATES,
        dtype=float,
    )
    w = momentum_target_weights(
        closes, n=1, lookback=3, skip=1, abs_filter=True, rebalance_month_change=False
    )
    # First full-lookback row is position 2; weights set there, not before
    assert w.iloc[0].tolist() == [0.0, 0.0, 0.0]
    assert w.iloc[1].tolist() == [0.0, 0.0, 0.0]
    assert w.iloc[2].tolist() == [1.0, 0.0, 0.0]


# -- vectorized trend (hand-verifiable) --------------------------------------

# Weekdays Jan 7 (Wed) through Jan 23 (Fri), 2026.
_TREND_DATES = pd.DatetimeIndex(
    [_dt(2026, 1, d) for d in (7, 8, 9, 12, 13, 14, 15, 16, 19, 20, 21, 22, 23)]
)


def test_trend_hand_verified_hysteresis():
    closes = pd.DataFrame(
        {"AAA": [100, 100, 106, 106, 106, 100, 100, 95, 95, 95, 95, 95, 95.5]},
        index=_TREND_DATES,
        dtype=float,
    )
    w = trend_target_weights(closes, ma=2, band_bps=100, check_weekday=4)
    # Fri Jan 9: ma 103, 106 > 104.03 -> in. Fri Jan 16: ma 97.5, 95 < 96.525 -> out.
    # Fri Jan 23: ma 95.25, 95.5 inside the band -> hysteresis holds the out state.
    expected = pd.DataFrame(
        {"AAA": [0, 0, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0]}, index=_TREND_DATES, dtype=float
    )
    pd.testing.assert_frame_equal(w, expected)


def test_trend_fixed_allocation_and_ma_warmup():
    closes = pd.DataFrame(
        {
            "AAA": [100, 100, 106, 106, 106, 100, 100, 95, 95, 95, 95, 95, 95.5],
            "BBB": [100.0] * 13,
        },
        index=_TREND_DATES,
    )
    w = trend_target_weights(closes, ma=2, band_bps=100, check_weekday=4)
    assert w.loc[_dt(2026, 1, 9)].tolist() == [0.5, 0.0]  # 1/len(symbols), BBB flat
    # ma=5 has only 3 bars by the first Friday: no state change despite the breakout
    w5 = trend_target_weights(closes[["AAA"]].iloc[:3], ma=5, band_bps=100, check_weekday=4)
    assert w5.to_numpy().sum() == 0.0


# -- parity: engine event-driven vs vectorized -------------------------------

_PARITY_DRIFTS = {"AAA": 0.0015, "BBB": 0.0005, "CCC": -0.0005, "DDD": 0.001}


def test_run_engine_strategy_harness_on_buyhold():
    bars, closes = _make_market({"AAA": 0.001}, n_bars=5)
    w = run_engine_strategy("buyhold", {"symbol": "AAA"}, bars)
    expected = pd.DataFrame({"AAA": [1.0] * 5}, index=closes.index)
    assert_parity(w, expected)


def test_assert_parity_reports_first_divergent_date():
    idx = pd.date_range("2020-01-01", periods=3, freq="D")
    a = pd.DataFrame({"AAA": [0.0, 0.5, 0.5]}, index=idx)
    b = pd.DataFrame({"AAA": [0.0, 0.25, 0.5]}, index=idx)
    with pytest.raises(AssertionError) as exc:
        assert_parity(a, b)
    msg = str(exc.value)
    assert "2020-01-02" in msg
    assert "AAA" in msg


def test_vectorized_strategies_active_on_parity_market():
    # Guards the parity tests against trivially passing on all-zero weights
    _, closes = _make_market(_PARITY_DRIFTS)
    mw = momentum_target_weights(closes, n=2, lookback=60, skip=5, abs_filter=True)
    tw = trend_target_weights(closes, ma=30, band_bps=100, check_weekday=4)
    assert mw.to_numpy().sum() > 0
    assert tw.to_numpy().sum() > 0
    assert (mw.sum(axis=1) <= 1.0 + 1e-9).all()
    assert (tw.sum(axis=1) <= 1.0 + 1e-9).all()


@pytest.mark.skipif(
    not strategy_registered("momentum_rotation"),
    reason="momentum_rotation not registered yet (concurrent agent)",
)
def test_parity_momentum_rotation():
    bars, closes = _make_market(_PARITY_DRIFTS)
    params = {
        "symbols": sorted(_PARITY_DRIFTS),
        "n": 2,
        "lookback": 60,
        "skip": 5,
        "abs_filter": True,
    }
    engine_w = run_engine_strategy("momentum_rotation", params, bars)
    vec_w = momentum_target_weights(closes, n=2, lookback=60, skip=5, abs_filter=True)
    assert engine_w.to_numpy().sum() > 0
    assert_parity(engine_w, vec_w)


@pytest.mark.skipif(
    not strategy_registered("trend_ma"),
    reason="trend_ma not registered yet (concurrent agent)",
)
def test_parity_trend_ma():
    bars, closes = _make_market(_PARITY_DRIFTS)
    params = {"symbols": sorted(_PARITY_DRIFTS), "ma": 30, "band_bps": 100, "check_weekday": 4}
    engine_w = run_engine_strategy("trend_ma", params, bars)
    vec_w = trend_target_weights(closes, ma=30, band_bps=100, check_weekday=4)
    assert engine_w.to_numpy().sum() > 0
    assert_parity(engine_w, vec_w)


# -- bootstrap ----------------------------------------------------------------


def test_sharpe_ci_contains_point_and_is_deterministic():
    rng = np.random.default_rng(0)
    returns = rng.normal(0.0005, 0.01, 750)
    lo, point, hi = sharpe_ci(returns)
    expected_point = float(np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(252.0))
    assert point == pytest.approx(expected_point)
    assert lo < hi
    assert lo <= point <= hi
    assert sharpe_ci(returns) == (lo, point, hi)
    lo7, point7, hi7 = sharpe_ci(returns, seed=7)
    assert point7 == point
    assert (lo7, hi7) != (lo, hi)


# -- walk-forward -------------------------------------------------------------


def test_rolling_splits_exact_boundaries():
    idx = pd.date_range("2020-01-01", periods=1500, freq="D")
    splits = rolling_splits(idx, train_years=2, test_years=1)
    assert splits == [
        (slice(0, 730), slice(730, 1095)),
        (slice(365, 1095), slice(1095, 1460)),
        (slice(730, 1460), slice(1460, 1500)),
    ]
    for tr, te in splits:
        assert tr.stop == te.start  # no train/test overlap, no gap
    for (_, t1), (_, t2) in zip(splits, splits[1:]):
        assert t1.stop == t2.start  # test windows tile
    assert splits[-1][1].stop == len(idx)


def test_expanding_splits_exact_boundaries():
    idx = pd.date_range("2020-01-01", periods=1500, freq="D")
    splits = expanding_splits(idx, train_years=2, test_years=1)
    assert [tr for tr, _ in splits] == [slice(0, 730), slice(0, 1095), slice(0, 1460)]
    assert [te for _, te in splits] == [
        slice(730, 1095),
        slice(1095, 1460),
        slice(1460, 1500),
    ]


def test_rolling_splits_step_override():
    idx = pd.date_range("2020-01-01", periods=1200, freq="D")
    splits = rolling_splits(idx, train_years=1, test_years=0.4, step_years=0.2)
    assert splits[0] == (slice(0, 365), slice(365, 511))
    assert splits[1] == (slice(73, 438), slice(438, 584))
    assert len(splits) > 2


def test_rolling_splits_full_coverage_on_gapped_index():
    days = pd.date_range("2019-01-01", periods=2100, freq="D")
    idx = pd.DatetimeIndex([d for d in days if d.weekday() < 5])
    splits = rolling_splits(idx, train_years=1.0, test_years=0.5)
    covered = np.concatenate([np.arange(te.start, te.stop) for _, te in splits])
    assert (covered == np.arange(splits[0][1].start, len(idx))).all()
