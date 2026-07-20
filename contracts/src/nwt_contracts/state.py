"""Trading state machine vocabulary.

Breakers and the watchdog may only move state toward safety (ACTIVE -> REDUCING
-> HALTED). Re-arming toward ACTIVE requires a human with typed confirmation and
per-latch acknowledgement; that flow lives in the risk layer, but the vocabulary
is shared here so the engine can enforce state locally as well.
"""

from enum import StrEnum


class TradingState(StrEnum):
    ACTIVE = "ACTIVE"      # all approved intents may flow
    REDUCING = "REDUCING"  # only reduce-only/protective intents may flow
    HALTED = "HALTED"      # nothing flows except cancels; kill/flatten always work

    def allows(self, *, reduces_position: bool) -> bool:
        if self is TradingState.ACTIVE:
            return True
        if self is TradingState.REDUCING:
            return reduces_position
        return False


#: Ordering for "toward safety" checks: transitions may only increase this rank
#: unless performed by a human through the confirmation flow.
SAFETY_RANK: dict[TradingState, int] = {
    TradingState.ACTIVE: 0,
    TradingState.REDUCING: 1,
    TradingState.HALTED: 2,
}


class HaltReason(StrEnum):
    STARTUP = "STARTUP"                        # every process start lands HALTED
    KILL_SWITCH = "KILL_SWITCH"
    DAILY_LOSS = "DAILY_LOSS"
    DRAWDOWN = "DRAWDOWN"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    REJECTION_STORM = "REJECTION_STORM"
    RECONCILE_MISMATCH = "RECONCILE_MISMATCH"
    EXTERNAL_ORDER = "EXTERNAL_ORDER"
    WATCHDOG = "WATCHDOG"
    CONFIG_CHANGE = "CONFIG_CHANGE"            # deploy/config change de-arms
    CLOCK_SKEW = "CLOCK_SKEW"
    OPERATOR = "OPERATOR"
