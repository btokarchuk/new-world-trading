"""Watchdog limits, loaded from config/watchdog.yaml.

Every number here sits deliberately WIDER than its counterpart in
config/risk.yaml. The watchdog is the second line: it must only fire once the
engine's own controls have already failed. Limits that overlap the engine's
would fire together, produce duplicate halts, and train the operator to ignore
the alert that actually means "the primary controls are gone".
"""

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel


class WatchdogConfig(BaseModel, frozen=True, extra="forbid"):
    # extra="forbid": a mistyped key must not silently leave the default limit
    # in force — a watchdog running limits the operator did not write is worse
    # than one that refuses to start.
    heartbeat_grace_s: int = 180
    # Repeat CRITICALs for the SAME standing breach are suppressed for this
    # long. One condition produced 315 identical pages overnight on
    # 2026-08-03; an alert that repeats every minute is an alert you learn to
    # ignore. Cancels still run every poll — only the paging backs off.
    page_backoff_s: int = 900
    poll_interval_s: int = 60
    max_open_orders: int = 15
    max_gross_notional_usd: Decimal = Decimal("9500")
    daily_pnl_floor_usd: Decimal = Decimal("-300")
    max_orders_per_10min: int = 15
    equity_floor_usd: Decimal = Decimal("8500")
    risk_db: Path = Path("data/risk.db")
    state_db: Path = Path("data/watchdog.db")
    healthcheck_url: str | None = None
    webhook_url: str | None = None
    dry_run: bool = False

    @classmethod
    def load(cls, path: Path | str) -> "WatchdogConfig":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)
