"""Pure catastrophe-stop coverage math, shared by paper and backtest.

The policy layer (risk/protect.py) adapts RiskConfig onto these functions; the
backtest runner calls them directly. One implementation, two submit sites —
the design's §5 point 5 warned that parallel arming logic WILL diverge, so
the logic lives here, below both, with explicit parameters and no config
dependency.

Design: docs/design/protective-stops.md. One resting sell stop per
(sleeve, symbol) lot at distance_pct below the lot's average cost, derived
from the LEDGER, never from bar data — the price must stay valid when the
data pipeline is what died.
"""

import hashlib
from decimal import ROUND_DOWN, Decimal
from typing import NamedTuple

from nwt_engine.domain import Universe
from nwt_engine.sleeves import SleeveLedger

_PREFIX = "prot"


class DesiredStop(NamedTuple):
    sleeve_id: str
    symbol: str
    qty: Decimal
    stop_price: Decimal
    client_order_id: str


def deterministic_protective_coid(
    sleeve_id: str, symbol: str, qty: Decimal, stop_price: Decimal
) -> str:
    """Encodes the LOT, not the moment: an unchanged lot re-derives the same
    id (idempotent re-arm after any wipe), a changed lot derives a new one
    (the diff cancels the stale stop)."""
    digest = hashlib.sha256(
        f"{sleeve_id}|{symbol}|{qty}|{stop_price}".encode()
    ).hexdigest()[:14]
    return f"{_PREFIX}-{sleeve_id}-{symbol.replace('/', '')}-{digest}"


def is_protective_coid(client_order_id: str) -> bool:
    return client_order_id.startswith(f"{_PREFIX}-")


def compute_desired_stops(
    ledgers: dict[str, SleeveLedger],
    universe: Universe,
    *,
    distance_pct: Decimal,
    exempt_sleeves: tuple[str, ...] = (),
) -> tuple[DesiredStop, ...]:
    """One stop per (sleeve, symbol) lot, priced off that lot's avg cost.

    Crypto is skipped in code, not config: Alpaca offers no stop type worth
    trusting there (design §7) — crypto exposure is bounded by sleeve size.
    """
    desired: list[DesiredStop] = []
    for sleeve_id in sorted(ledgers):
        if sleeve_id in exempt_sleeves:
            continue
        ledger = ledgers[sleeve_id]
        for symbol in sorted(ledger.positions):
            qty, avg_cost = ledger.positions[symbol]
            if qty <= 0 or avg_cost <= 0:
                continue
            try:
                inst = universe.get(symbol)
            except KeyError:
                continue
            if inst.asset_class.value == "crypto":
                continue
            stop_price = (avg_cost * (1 - distance_pct / 100)).quantize(
                inst.tick_size, rounding=ROUND_DOWN
            )
            if stop_price <= 0:
                continue
            desired.append(
                DesiredStop(
                    sleeve_id=sleeve_id,
                    symbol=symbol,
                    qty=qty,
                    stop_price=stop_price,
                    client_order_id=deterministic_protective_coid(
                        sleeve_id, symbol, qty, stop_price
                    ),
                )
            )
    return tuple(desired)


def diff_protection(
    desired: tuple[DesiredStop, ...],
    resting_protective_coids: set[str],
) -> tuple[tuple[DesiredStop, ...], tuple[str, ...]]:
    """(arms, cancels): what is missing, and what rests but matches nothing.

    Matching is by coid — which encodes (sleeve, symbol, qty, stop_price) —
    so any lot drift means cancel + re-arm, and an unchanged lot is a no-op.
    """
    desired_ids = {stop.client_order_id for stop in desired}
    arms = tuple(s for s in desired if s.client_order_id not in resting_protective_coids)
    cancels = tuple(sorted(resting_protective_coids - desired_ids))
    return arms, cancels
