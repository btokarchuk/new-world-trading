"""Pre-trade check catalog.

default_checks() returns the catalog in its canonical evaluation order. The
governor runs EVERY check regardless of earlier results (full reason set in
the audit log), so ordering only shapes audit readability: the state gate
leads, cheap data-trust and eligibility rejects come early, and the
portfolio-exposure math runs last.
"""

from .base import CheckResult, PreTradeCheck, allow, clamp, reject
from .cooldown import CooldownCheck
from .duplicates import DuplicateCheck
from .exposure import ExposureCheck
from .long_only import LongOnlyCheck
from .order_size import OrderSizeCheck
from .price_collar import PriceCollarCheck
from .rate_limit import RateLimitCheck
from .session import SessionCheck
from .staleness import StalenessCheck
from .state_gate import StateGateCheck
from .universe import UniverseCheck

__all__ = [
    "CheckResult",
    "CooldownCheck",
    "DuplicateCheck",
    "ExposureCheck",
    "LongOnlyCheck",
    "OrderSizeCheck",
    "PreTradeCheck",
    "PriceCollarCheck",
    "RateLimitCheck",
    "SessionCheck",
    "StalenessCheck",
    "StateGateCheck",
    "UniverseCheck",
    "allow",
    "clamp",
    "default_checks",
    "reject",
]


def default_checks() -> list[PreTradeCheck]:
    return [
        StateGateCheck(),
        SessionCheck(),
        StalenessCheck(),
        UniverseCheck(),
        CooldownCheck(),
        DuplicateCheck(),
        RateLimitCheck(),
        LongOnlyCheck(),
        OrderSizeCheck(),
        PriceCollarCheck(),
        ExposureCheck(),
    ]
