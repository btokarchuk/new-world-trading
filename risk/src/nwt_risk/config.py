"""Risk configuration: every limit is a hard block; the config hash is asserted
at startup and any change while live-armed de-arms to paper+HALTED."""

import hashlib
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator


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


class ProtectionRules(BaseModel, frozen=True):
    """Catastrophe stops (docs/design/protective-stops.md). NOT a trading rule:
    the distance is chosen to never fire while the system is alive — its only
    job is bounding loss when the machine is dead with positions open.

    CHANGING distance_pct INVALIDATES the no-op backtest re-run and the entire
    never-fires-in-sample argument (§6). Re-derive, re-run, re-certify.
    """

    enabled: bool = True
    distance_pct: Decimal = Decimal("25")
    # Two-sided sanity band around the intended distance, measured from the
    # lot's avg cost: rejects fat-fingered tactical stops (1% away) as hard as
    # runaway ones (40% away). Design §4 phase 4 point 1.
    band_min_pct: Decimal = Decimal("20")
    band_max_pct: Decimal = Decimal("30")
    # Sleeves whose positions deliberately carry NO stop (owner decision:
    # control is the benchmark; a stopped benchmark is not a benchmark).
    exempt_sleeves: tuple[str, ...] = ()
    # A protective stop FIRING halts the whole system (owner decision §8 row
    # 5): by construction it is an event outside the entire sample.
    halt_on_fire: bool = True

    @model_validator(mode="after")
    def _band_contains_distance(self) -> "ProtectionRules":
        if not self.band_min_pct <= self.distance_pct <= self.band_max_pct:
            raise ValueError("protection band must contain distance_pct")
        return self


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
    protection: ProtectionRules = ProtectionRules()

    config_hash: str = ""

    @classmethod
    def load(cls, path: Path | str) -> "RiskConfig":
        raw_bytes = Path(path).read_bytes()
        data = yaml.safe_load(raw_bytes)
        cfg = cls.model_validate({**data, "config_hash": ""})
        digest = hashlib.sha256(raw_bytes).hexdigest()
        return cfg.model_copy(update={"config_hash": digest})
