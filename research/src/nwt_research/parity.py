"""Signal-parity harness: engine strategy classes vs vectorized references.

The guard against "backtested one thing, deployed another": the event-driven
strategy and its vectorized re-implementation must propose identical target
weights on the same bars. Only weights are compared — no sizing, cash, or
fill accounting is simulated here.
"""

import importlib
from decimal import Decimal

import pandas as pd

from nwt_contracts import PortfolioView, PositionView

from nwt_engine.domain import Bar, TargetWeight
from nwt_engine.strategies import HistoryView, StrategyContext
from nwt_engine.strategies.registry import get_strategy

_CLASSICAL_MODULES = ("buyhold", "momentum", "trend", "meanrev")


def _ensure_registered() -> None:
    # nwt_engine.strategies registers what its __init__ wires in; classical
    # modules not yet exported there are imported individually (idempotent —
    # the import cache prevents double registration).
    import nwt_engine.strategies  # noqa: F401

    for mod in _CLASSICAL_MODULES:
        try:
            importlib.import_module(f"nwt_engine.strategies.classical.{mod}")
        except ImportError:
            pass


def strategy_registered(name: str) -> bool:
    _ensure_registered()
    try:
        get_strategy(name)
    except KeyError:
        return False
    return True


def run_engine_strategy(
    strategy_name: str,
    params: dict,
    bars_by_symbol: dict[str, list[Bar]],
) -> pd.DataFrame:
    """Walk every bar-close timestamp through the engine strategy class.

    Maintains a running target-weights dict: each TargetWeight proposal
    overwrites its symbol's entry (Trade proposals are out of scope for
    parity and ignored). The virtual sleeve mirrors those weights back as
    positions — strategies derive their in/out state from sleeve holdings,
    so an always-empty sleeve would suppress exit and rotation proposals.
    Returns one float row per decision timestamp.
    """
    _ensure_registered()
    cls = get_strategy(strategy_name)
    strategy = cls()
    params_obj = cls.params_model(**params)

    timestamps = sorted({b.ts_close for bars in bars_by_symbol.values() for b in bars})
    weights: dict[str, float] = {sym: 0.0 for sym in bars_by_symbol}
    rows: list[dict[str, float]] = []
    started = False
    for ts in timestamps:
        positions = tuple(
            PositionView(symbol=sym, qty=Decimal("1"), avg_cost=Decimal("1"))
            for sym in sorted(weights)
            if weights[sym] > 0
        )
        sleeve = PortfolioView(
            scope="research",
            ts=ts,
            cash=Decimal("100000"),
            equity=Decimal("100000"),
            positions=positions,
        )
        ctx = StrategyContext(
            now=ts,
            sleeve=sleeve,
            history=HistoryView(bars_by_symbol, ts),
            params=params_obj,
        )
        if not started:
            strategy.on_start(ctx)
            started = True
        for proposal in strategy.on_schedule(ctx):
            action = proposal.action
            if isinstance(action, TargetWeight):
                weights[action.symbol] = float(action.weight)
        rows.append(dict(weights))
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(timestamps))
    return df.reindex(columns=sorted(df.columns)).fillna(0.0)


def assert_parity(
    engine_weights: pd.DataFrame,
    vectorized_weights: pd.DataFrame,
    tol: float = 1e-9,
) -> None:
    """Raise AssertionError with the first divergent date if weights differ."""
    e = engine_weights.sort_index()
    v = vectorized_weights.sort_index()
    if set(e.columns) != set(v.columns):
        raise AssertionError(
            f"column mismatch: engine={sorted(e.columns)} vectorized={sorted(v.columns)}"
        )
    v = v[e.columns]
    if not e.index.equals(v.index):
        raise AssertionError(
            "index mismatch: "
            f"engine {len(e.index)} rows [{e.index[0]} .. {e.index[-1]}], "
            f"vectorized {len(v.index)} rows [{v.index[0]} .. {v.index[-1]}]"
        )
    diff = (e - v).abs()
    bad = diff.gt(tol).any(axis=1)
    if bad.any():
        ts = bad.idxmax()
        lines = [f"weights diverge at {ts} (tol={tol}):"]
        for sym in e.columns:
            ev = float(e.at[ts, sym])
            vv = float(v.at[ts, sym])
            marker = "   <-- differs" if abs(ev - vv) > tol else ""
            lines.append(f"  {sym}: engine={ev:.12f} vectorized={vv:.12f}{marker}")
        lines.append(f"({int(bad.sum())} of {len(e)} rows diverge)")
        raise AssertionError("\n".join(lines))
