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


def compute_relative_metrics(
    equity: list[float], benchmark: list[float]
) -> dict[str, float]:
    """Alpha/beta vs the control sleeve via OLS on daily returns, plus tracking
    error and information ratio. The control shares the strategy's exact cost
    treatment, which is what makes these numbers honest."""
    n = min(len(equity), len(benchmark))
    if n < 3:
        return {}
    rs = [equity[i] / equity[i - 1] - 1 for i in range(1, n)]
    rb = [benchmark[i] / benchmark[i - 1] - 1 for i in range(1, n)]
    m = len(rs)
    mean_s = sum(rs) / m
    mean_b = sum(rb) / m
    cov = sum((rs[i] - mean_s) * (rb[i] - mean_b) for i in range(m)) / m
    var_b = sum((r - mean_b) ** 2 for r in rb) / m
    beta = cov / var_b if var_b > 0 else 0.0
    alpha_daily = mean_s - beta * mean_b
    active = [rs[i] - rb[i] for i in range(m)]
    mean_active = sum(active) / m
    te_var = sum((a - mean_active) ** 2 for a in active) / m
    tracking_error = math.sqrt(te_var) * math.sqrt(TRADING_DAYS)
    info_ratio = (mean_active * TRADING_DAYS) / tracking_error if tracking_error > 0 else 0.0
    return {
        "beta_vs_control": beta,
        "alpha_annualized": alpha_daily * TRADING_DAYS,
        "tracking_error": tracking_error,
        "information_ratio": info_ratio,
        "active_return_annualized": mean_active * TRADING_DAYS,
    }
