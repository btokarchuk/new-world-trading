"""Performance metrics from a daily equity series. Float math is fine here —
accounting is Decimal; statistics are statistics."""

import math

TRADING_DAYS = 252


def compute_metrics(equity: list[float]) -> dict[str, float]:
    if len(equity) < 2:
        return {"total_return": 0.0}
    returns = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    n = len(returns)
    total_return = equity[-1] / equity[0] - 1
    years = n / TRADING_DAYS
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1 if years > 0 else 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n if n > 1 else 0.0
    vol = math.sqrt(var) * math.sqrt(TRADING_DAYS)
    sharpe = (mean * TRADING_DAYS) / vol if vol > 0 else 0.0
    downside = [r for r in returns if r < 0]
    dvar = sum(r**2 for r in downside) / n if downside else 0.0
    dvol = math.sqrt(dvar) * math.sqrt(TRADING_DAYS)
    sortino = (mean * TRADING_DAYS) / dvol if dvol > 0 else 0.0
    peak, max_dd = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        max_dd = max(max_dd, 1 - value / peak)
    calmar = cagr / max_dd if max_dd > 0 else 0.0
    return {
        "total_return": total_return,
        "cagr": cagr,
        "vol": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "calmar": calmar,
    }
