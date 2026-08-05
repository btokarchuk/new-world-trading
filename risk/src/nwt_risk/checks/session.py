"""Session gating: NYSE regular hours for equities, a local supervision
window for crypto (a 24/7 venue we only trade while awake to watch it)."""

from datetime import time
from typing import ClassVar
from zoneinfo import ZoneInfo

from nwt_contracts import AssetClass, OrderIntent, SessionInfo

from ..config import RiskConfig
from ..context import GovernorContext
from ..reasons import ReasonCode
from .base import CheckResult, allow, reject

ET = ZoneInfo("America/New_York")


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


class SessionCheck:
    name: ClassVar[str] = "session"

    def evaluate(
        self, intent: OrderIntent, ctx: GovernorContext, cfg: RiskConfig
    ) -> CheckResult:
        local = ctx.now.astimezone(ET)
        if intent.asset_class in (AssetClass.EQUITY, AssetClass.ETF):
            return self._equity(intent, ctx, cfg, local)
        return self._crypto(intent, cfg, local)

    def _equity(self, intent, ctx, cfg, local) -> CheckResult:
        # Protective SELL stops arm at any hour: the 16:05 EOD poll must be
        # able to protect a late fill rather than leave it naked overnight,
        # and a GTC stop is not an execution now — it is a resting instruction
        # the exchange holds. Scoped hard to the contract-validated protective
        # shape (SELL + reduces + stop, enforced at construction).
        if intent.is_protective:
            return allow(self.name)
        xnys: SessionInfo | None = next(
            (s for s in ctx.base.sessions if s.calendar == "XNYS"), None
        )
        is_open = xnys is not None and xnys.is_open
        if cfg.session.regular_hours_only and not is_open:
            return reject(self.name, ReasonCode.SESSION_CLOSED, "XNYS not open")
        cutoff = _parse_hhmm(cfg.session.no_new_entries_after_et)
        if is_open and not intent.reduces_position and local.time() >= cutoff:
            return reject(
                self.name,
                ReasonCode.ENTRY_CUTOFF,
                f"no new entries after {cfg.session.no_new_entries_after_et} ET",
            )
        return allow(self.name)

    def _crypto(self, intent, cfg, local) -> CheckResult:
        if intent.is_protective:
            return allow(self.name)  # stops/exits must always be able to flow
        start, end = (_parse_hhmm(v) for v in cfg.session.crypto_window_local)
        if not (start <= local.time() < end):
            return reject(
                self.name,
                ReasonCode.CRYPTO_WINDOW,
                f"local {local.time().isoformat('minutes')} outside {start}-{end}",
            )
        return allow(self.name)
