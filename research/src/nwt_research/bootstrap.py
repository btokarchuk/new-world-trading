"""Block-bootstrap confidence intervals for the Sharpe ratio.

Research tooling: seeded randomness is allowed here (the engine determinism
rule applies to decision/journal paths, not offline statistics).
"""

import numpy as np

_ANNUALIZER = 252.0


def _sharpe(r: np.ndarray) -> float:
    sd = r.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(r.mean() / sd * np.sqrt(_ANNUALIZER))


def sharpe_ci(
    returns: np.ndarray,
    block: int = 20,
    n_boot: int = 2000,
    alpha: float = 0.10,
    seed: int = 42,
) -> tuple[float, float, float]:
    """(lo, point, hi) for the annualized Sharpe via circular block bootstrap.

    Blocks of `block` consecutive daily returns are drawn with wraparound until
    each resample has the original length; the CI is the (alpha/2, 1 - alpha/2)
    percentile interval of the resampled Sharpes. Deterministic for a given seed.
    """
    r = np.asarray(returns, dtype=float).ravel()
    n = r.size
    if n < 2:
        raise ValueError("need at least 2 returns")
    if block < 1 or n_boot < 1:
        raise ValueError("block and n_boot must be >= 1")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0, 1)")

    point = _sharpe(r)
    rng = np.random.default_rng(seed)
    n_blocks = -(-n // block)
    starts = rng.integers(0, n, size=(n_boot, n_blocks))
    idx = (starts[..., None] + np.arange(block)) % n
    samples = r[idx.reshape(n_boot, -1)[:, :n]]

    mu = samples.mean(axis=1)
    sd = samples.std(axis=1, ddof=1)
    sd_safe = np.where(sd > 0, sd, 1.0)
    stats = np.where(sd > 0, mu / sd_safe * np.sqrt(_ANNUALIZER), 0.0)
    lo, hi = np.quantile(stats, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(lo), float(point), float(hi)
