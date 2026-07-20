"""Unit tests for the classical strategies, driven directly with hand-built
bars — no runner. Price series are chosen so momentum/MA/z values are exact
and verifiable by inspection."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from nwt_contracts import PortfolioView, PositionView, Side

from nwt_engine.domain import Bar, TargetWeight, Timeframe, Trade
from nwt_engine.strategies import HistoryView, StrategyContext
from nwt_engine.strategies.classical.meanrev import MeanRevZScore, MeanRevZScoreParams
from nwt_engine.strategies.classical.momentum import (
    MomentumRotation,
    MomentumRotationParams,
)
from nwt_engine.strategies.classical.trend import TrendMA, TrendMAParams

UTC = timezone.utc

# Fridays in Jan/Feb 2024 (weekday() == 4) used as decision timestamps.
JAN5 = datetime(2024, 1, 5, 21, 0, tzinfo=UTC)
JAN4 = datetime(2024, 1, 4, 21, 0, tzinfo=UTC)  # Thursday
FEB2 = datetime(2024, 2, 2, 21, 0, tzinfo=UTC)


def mk_bars(symbol: str, closes: list, end: datetime) -> list[Bar]:
    """One daily bar per close, the last closing exactly at `end`."""
    out = []
    for i, c in enumerate(closes):
        ts_close = end - timedelta(days=len(closes) - 1 - i)
        cd = Decimal(str(c))
        out.append(
            Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                ts_open=ts_close - timedelta(hours=7),
                ts_close=ts_close,
                open=cd,
                high=cd,
                low=cd,
                close=cd,
                volume=Decimal("1000"),
            )
        )
    return out


def mk_ctx(now, bars_by_symbol, params, positions=(), equity="10000"):
    pos = tuple(
        PositionView(symbol=s, qty=Decimal(str(q)), avg_cost=Decimal(str(a)))
        for s, q, a in positions
    )
    sleeve = PortfolioView(
        scope="test", ts=now, cash=Decimal("0"),
        equity=Decimal(str(equity)), positions=pos,
    )
    return StrategyContext(now, sleeve, HistoryView(bars_by_symbol, now), params)


def by_symbol(proposals):
    return {p.action.symbol: p.action for p in proposals}


# --- momentum_rotation -----------------------------------------------------
# lookback=5, skip=1: momentum = closes[-2] / closes[-5] - 1.
#   A: [100, 105, 110, 120, 1]   -> 120/100 - 1 = 0.20 (crash last bar skipped)
#   B: [100, 102, 104, 110, 200] -> 110/100 - 1 = 0.10 (spike last bar skipped)
#   C: [100, 99, 98, 95, 500]    -> 95/100  - 1 = -0.05

MOM_CLOSES = {
    "A": [100, 105, 110, 120, 1],
    "B": [100, 102, 104, 110, 200],
    "C": [100, 99, 98, 95, 500],
}


def mom_history(end, symbols=("A", "B", "C")):
    return {s: mk_bars(s, MOM_CLOSES[s], end) for s in symbols}


def mom_params(**kw):
    return MomentumRotationParams(
        symbols=kw.pop("symbols", ["A", "B", "C"]), n=2, lookback=5, skip=1, **kw
    )


def test_momentum_ranks_top_n_with_skip():
    strat = MomentumRotation()
    props = strat.on_schedule(mk_ctx(JAN5, mom_history(JAN5), mom_params()))
    actions = by_symbol(props)
    assert set(actions) == {"A", "B"}  # C ranks last, not held -> nothing emitted
    assert actions["A"].weight == Decimal("0.5")
    assert actions["B"].weight == Decimal("0.5")
    assert all(isinstance(p.action, TargetWeight) for p in props)


def test_momentum_rebalances_only_on_month_change():
    strat = MomentumRotation()
    params = mom_params()
    assert strat.on_schedule(mk_ctx(JAN5, mom_history(JAN5), params))  # first decision
    jan8 = JAN5 + timedelta(days=3)
    assert strat.on_schedule(mk_ctx(jan8, mom_history(jan8), params)) == []
    assert strat.on_schedule(mk_ctx(FEB2, mom_history(FEB2), params))  # new month
    feb3 = FEB2 + timedelta(days=1)
    assert strat.on_schedule(mk_ctx(feb3, mom_history(feb3), params)) == []


def test_momentum_abs_filter_zeroes_negative_pick():
    history = mom_history(JAN5, symbols=("A", "C"))
    # C is top-2 by construction (only two symbols) but momentum is -0.05.
    props = MomentumRotation().on_schedule(
        mk_ctx(JAN5, history, mom_params(symbols=["A", "C"]))
    )
    assert set(by_symbol(props)) == {"A"}  # C filtered to cash; not held -> no emit

    props = MomentumRotation().on_schedule(
        mk_ctx(JAN5, history, mom_params(symbols=["A", "C"], abs_filter=False))
    )
    actions = by_symbol(props)
    assert set(actions) == {"A", "C"}
    assert actions["C"].weight == Decimal("0.5")


def test_momentum_rotation_emits_only_changes():
    # Holding B and C; winners are A and B -> enter A, exit C, B untouched.
    props = MomentumRotation().on_schedule(
        mk_ctx(
            JAN5, mom_history(JAN5), mom_params(),
            positions=[("B", 10, 100), ("C", 10, 95)],
        )
    )
    actions = by_symbol(props)
    assert set(actions) == {"A", "C"}
    assert actions["A"].weight == Decimal("0.5")
    assert actions["C"].weight == Decimal("0")


def test_momentum_insufficient_history_unrankable_and_exited():
    history = mom_history(JAN5, symbols=("A", "B"))
    history["D"] = mk_bars("D", [100, 200, 300], JAN5)  # 3 < lookback=5
    props = MomentumRotation().on_schedule(
        mk_ctx(
            JAN5, history, mom_params(symbols=["A", "B", "D"]),
            positions=[("D", 10, 100)],
        )
    )
    actions = by_symbol(props)
    # D would win on raw momentum if rankable; instead it is excluded and exited.
    assert set(actions) == {"A", "B", "D"}
    assert actions["D"].weight == Decimal("0")


# --- trend_ma --------------------------------------------------------------
# ma=3, band_bps=100 (1%). MA and band thresholds are exact by construction.

def trend_params(symbols=None, **kw):
    return TrendMAParams(symbols=symbols or ["X"], ma=3, band_bps=100, **kw)


def test_trend_enters_above_band():
    # closes [100, 100, 103]: MA=101, upper=102.01, close 103 > upper -> enter.
    history = {"X": mk_bars("X", [100, 100, 103], JAN5)}
    props = TrendMA().on_schedule(mk_ctx(JAN5, history, trend_params()))
    actions = by_symbol(props)
    assert set(actions) == {"X"}
    assert actions["X"].weight == Decimal("1")


def test_trend_holds_inside_band():
    # closes [100, 101, 102]: MA=101, band 99.99..102.01, close 102 inside.
    history = {"X": mk_bars("X", [100, 101, 102], JAN5)}
    assert TrendMA().on_schedule(mk_ctx(JAN5, history, trend_params())) == []
    assert (
        TrendMA().on_schedule(
            mk_ctx(JAN5, history, trend_params(), positions=[("X", 10, 100)])
        )
        == []
    )


def test_trend_exits_below_band():
    # closes [100, 100, 94]: MA=98, lower=97.02, close 94 < lower -> exit.
    history = {"X": mk_bars("X", [100, 100, 94], JAN5)}
    props = TrendMA().on_schedule(
        mk_ctx(JAN5, history, trend_params(), positions=[("X", 10, 100)])
    )
    actions = by_symbol(props)
    assert set(actions) == {"X"}
    assert actions["X"].weight == Decimal("0")
    # Same tape while flat: no proposal (emit only changes).
    assert TrendMA().on_schedule(mk_ctx(JAN5, history, trend_params())) == []


def test_trend_acts_only_on_check_weekday():
    history = {"X": mk_bars("X", [100, 100, 103], JAN4)}
    assert TrendMA().on_schedule(mk_ctx(JAN4, history, trend_params())) == []  # Thursday


def test_trend_fixed_fraction_and_insufficient_history():
    history = {
        "X": mk_bars("X", [100, 100, 103], JAN5),
        "Y": mk_bars("Y", [100, 103], JAN5),  # 2 < ma=3
    }
    props = TrendMA().on_schedule(mk_ctx(JAN5, history, trend_params(["X", "Y"])))
    actions = by_symbol(props)
    assert set(actions) == {"X"}
    assert actions["X"].weight == Decimal("0.5")  # 1/len(symbols), not 1/entered


# --- meanrev_zscore --------------------------------------------------------
# lookback=5. Entry tape [100,100,100,100,90]: mean=98, pop std=4, z=-2.0.
# Hold tape  [100,96,100,100,98]: mean=98.8, pop std=1.6, z=-0.5 (between).
# Exit tape  [90,110,100,100,100]: mean=100, close=100 -> z=0.0.

ENTRY_TAPE = [100, 100, 100, 100, 90]
HOLD_TAPE = [100, 96, 100, 100, 98]
EXIT_TAPE = [90, 110, 100, 100, 100]


def mr_params(**kw):
    kw.setdefault("symbols", ["A"])
    kw.setdefault("lookback", 5)
    kw.setdefault("time_stop_days", 3)
    return MeanRevZScoreParams(**kw)


def test_meanrev_enters_at_entry_z_with_sized_limit_order():
    history = {"A": mk_bars("A", ENTRY_TAPE, JAN5)}
    props = MeanRevZScore().on_schedule(mk_ctx(JAN5, history, mr_params()))
    assert len(props) == 1
    trade = props[0].action
    assert isinstance(trade, Trade)
    assert trade.side is Side.BUY
    assert trade.qty == Decimal("22")  # floor(10000 / 5 / 90)
    assert trade.limit_hint == Decimal("90")


def test_meanrev_no_entry_above_entry_z_and_skips_zero_std():
    assert (
        MeanRevZScore().on_schedule(
            mk_ctx(JAN5, {"A": mk_bars("A", HOLD_TAPE, JAN5)}, mr_params())
        )
        == []
    )
    flat = {"A": mk_bars("A", [100] * 5, JAN5)}  # std == 0
    assert MeanRevZScore().on_schedule(mk_ctx(JAN5, flat, mr_params())) == []


def test_meanrev_exits_on_exit_z_with_held_qty():
    history = {"A": mk_bars("A", EXIT_TAPE, JAN5)}
    props = MeanRevZScore().on_schedule(
        mk_ctx(JAN5, history, mr_params(), positions=[("A", 22, 90)])
    )
    assert len(props) == 1
    trade = props[0].action
    assert trade.side is Side.SELL
    assert trade.qty == Decimal("22")
    assert trade.limit_hint == Decimal("100")


def test_meanrev_time_stop_counts_decisions_and_resets_on_exit():
    strat = MeanRevZScore()
    params = mr_params()  # time_stop_days=3

    def decide(day, tape, positions=()):
        now = JAN5 + timedelta(days=day)
        return strat.on_schedule(
            mk_ctx(now, {"A": mk_bars("A", tape, now)}, params, positions)
        )

    held = [("A", 22, 90)]
    assert len(decide(0, ENTRY_TAPE)) == 1                # decision 1: enter
    assert decide(1, HOLD_TAPE, held) == []               # age 1, z between
    assert decide(2, HOLD_TAPE, held) == []               # age 2
    props = decide(3, HOLD_TAPE, held)                    # age 3 -> time stop
    assert len(props) == 1 and props[0].action.side is Side.SELL
    # Entry age was cleared on exit: re-entry starts a fresh clock.
    assert len(decide(4, ENTRY_TAPE)) == 1                # decision 5: re-enter
    assert decide(5, HOLD_TAPE, held) == []               # age 1 again, no stop


def test_meanrev_respects_max_positions():
    params = mr_params(symbols=["A", "B", "C"], max_positions=2)
    history = {s: mk_bars(s, ENTRY_TAPE, JAN5) for s in ("A", "B", "C")}
    props = MeanRevZScore().on_schedule(mk_ctx(JAN5, history, params))
    assert [p.action.symbol for p in props] == ["A", "B"]  # sorted, capped at 2
    assert all(p.action.qty == Decimal("55") for p in props)  # floor(10000/2/90)

    # A held (z between -> no exit) counts toward the cap: one slot left.
    history["A"] = mk_bars("A", HOLD_TAPE, JAN5)
    props = MeanRevZScore().on_schedule(
        mk_ctx(JAN5, history, params, positions=[("A", 55, 90)])
    )
    assert [p.action.symbol for p in props] == ["B"]


def test_meanrev_skips_entry_when_qty_floors_to_zero():
    history = {"A": mk_bars("A", ENTRY_TAPE, JAN5)}
    props = MeanRevZScore().on_schedule(
        mk_ctx(JAN5, history, mr_params(), equity="100")  # 100/5/90 -> 0 shares
    )
    assert props == []
