"""RiskConfig adapter over the shared protection core.

The math lives in nwt_engine.execution.protection — shared with the backtest
runner so the two submit sites cannot diverge (design §5 point 5). This module
adds only the policy binding: which config fields feed the pure functions.
"""

from typing import NamedTuple

from nwt_contracts import OrderRef, Side

from nwt_engine.domain import Universe
from nwt_engine.execution.protection import (
    DesiredStop,
    compute_desired_stops,
    deterministic_protective_coid,
    diff_protection,
    is_protective_coid,
)
from nwt_engine.sleeves import SleeveLedger

from .config import RiskConfig

__all__ = [
    "DesiredStop",
    "ProtectionPlan",
    "deterministic_protective_coid",
    "is_protective_coid",
    "plan_protection",
]


class ProtectionPlan(NamedTuple):
    arms: tuple[DesiredStop, ...]
    cancels: tuple[str, ...]


def plan_protection(
    ledgers: dict[str, SleeveLedger],
    open_orders: tuple[OrderRef, ...],
    universe: Universe,
    cfg: RiskConfig,
) -> ProtectionPlan:
    """Diff desired coverage against what actually rests at the broker.

    Declarative on purpose: it never asks WHY coverage is missing (watchdog
    cancel_all, kill, 90-day GTC expiry, crash between fill and arm, positions
    predating the feature) — it re-arms the difference, every cycle. Observed
    need 2026-08-05 06:12: a watchdog cancel stripped a resting stop and
    nothing put it back. Non-protective orders are invisible here.
    """
    desired = (
        compute_desired_stops(
            ledgers,
            universe,
            distance_pct=cfg.protection.distance_pct,
            exempt_sleeves=cfg.protection.exempt_sleeves,
        )
        if cfg.protection.enabled
        else ()
    )
    resting = {
        ref.client_order_id
        for ref in open_orders
        if is_protective_coid(ref.client_order_id) and ref.side is Side.SELL
    }
    arms, cancels = diff_protection(desired, resting)
    return ProtectionPlan(arms=arms, cancels=cancels)
