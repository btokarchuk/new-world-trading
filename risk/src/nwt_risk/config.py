"""Risk configuration: every limit is a hard block; the config hash is asserted
at startup and any change while live-armed de-arms to paper+HALTED."""

import hashlib
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel


class OrderLimits(BaseModel, frozen=True):
    max_notional_usd: Decimal = Decimal("500")
    max_shares: Decimal = Decimal("200")
    min_notional_usd: Decimal = Decimal("25")
    price_collar_pct_equity: Decimal = Decimal("2.0")
    price_collar_pct_crypto: Decimal = Decimal("5.0")
    suspect_quote_divergence_pct: Decimal = Decimal("1.0")


class ExposureLimits(BaseModel, frozen=True):
    max_symbol_notional_usd: Decimal = Decimal("1000")
    max_gross_notional_usd: Decimal = Decimal("9000")
    max_position_count: int = 12
    crypto_sleeve_max_usd: Decimal = Decimal("750")
    min_price_usd: Decimal = Decimal("5.00")
    max_pct_adv: Decimal = Decimal("0.01")


class DuplicateRules(BaseModel, frozen=True):
    rolling_window_s: int = 90
    one_open_entry_per_symbol_per_sleeve: bool = True


class RateLimits(BaseModel, frozen=True):
    global_per_min: int = 5
    global_per_day: int = 40
    per_symbol_per_min: int = 2
    per_symbol_per_day: int = 6


class SessionRules(BaseModel, frozen=True):
    regular_hours_only: bool = True
    no_new_entries_after_et: str = "15:45"
    crypto_window_local: tuple[str, str] = ("08:00", "22:00")


class StalenessRules(BaseModel, frozen=True):
    max_quote_age_s: int = 30
    max_reconcile_age_s: int = 300
    max_clock_skew_s: int = 2


class SleeveBudget(BaseModel, frozen=True):
    budget_usd: Decimal
    max_order_usd: Decimal
    live_enabled: bool = True


class BreakerLimits(BaseModel, frozen=True):
    daily_loss_usd: Decimal = Decimal("200")
    drawdown_warn_pct: Decimal = Decimal("6.0")
    drawdown_halt_pct: Decimal = Decimal("10.0")
    consecutive_losses: int = 4
    consecutive_window_h: int = 48
    cool_off_h: int = 24
    cooldown_after_exit_h: int = 4
    cooldown_after_stop_h: int = 24
    rejection_count: int = 5
    rejection_window_min: int = 10


class ReconcileRules(BaseModel, frozen=True):
    startup_delay_s: int = 10
    interval_s: int = 60
    in_flight_grace_s: int = 5
    cash_tolerance_usd: Decimal = Decimal("5.00")
    crypto_qty_rel_tolerance: Decimal = Decimal("0.000001")


class RiskConfig(BaseModel, frozen=True):
    equity_reference_usd: Decimal = Decimal("10000")
    order: OrderLimits = OrderLimits()
    exposure: ExposureLimits = ExposureLimits()
    duplicates: DuplicateRules = DuplicateRules()
    rate: RateLimits = RateLimits()
    session: SessionRules = SessionRules()
    staleness: StalenessRules = StalenessRules()
    sleeves: dict[str, SleeveBudget] = {}
    breakers: BreakerLimits = BreakerLimits()
    reconcile: ReconcileRules = ReconcileRules()

    config_hash: str = ""

    @classmethod
    def load(cls, path: Path | str) -> "RiskConfig":
        raw_bytes = Path(path).read_bytes()
        data = yaml.safe_load(raw_bytes)
        cfg = cls.model_validate({**data, "config_hash": ""})
        digest = hashlib.sha256(raw_bytes).hexdigest()
        return cfg.model_copy(update={"config_hash": digest})
