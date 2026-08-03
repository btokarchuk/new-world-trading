from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nwt_watchdog import invariants
from nwt_watchdog.config import WatchdogConfig

NOW = datetime(2026, 8, 3, 14, 30, tzinfo=UTC)
CONFIG = WatchdogConfig()  # the shipped defaults ARE the limits under test
REPO = Path(__file__).resolve().parents[2]


def _heartbeat(late_s: int) -> dict:
    next_due = NOW - timedelta(seconds=late_s)
    return {
        "seq": 7,
        "ts": (next_due - timedelta(seconds=30)).isoformat(),
        "next_due": next_due.isoformat(),
        "phase": "cycle",
        "detail": "",
    }


def _orders(count: int, age_s: int = 60) -> list[dict]:
    created = (NOW - timedelta(seconds=age_s)).isoformat()
    return [{"id": f"o{i}", "symbol": "SPY", "created_at": created} for i in range(count)]


def _positions(*market_values: str) -> list[dict]:
    return [
        {"symbol": f"S{i}", "qty": "1", "market_value": value}
        for i, value in enumerate(market_values)
    ]


def _account(equity: str, last_equity: str = "10000") -> dict:
    return {"equity": equity, "last_equity": last_equity, "cash": "500"}


# -- heartbeat --------------------------------------------------------------


@pytest.mark.parametrize(
    "late_s, breached",
    [(-3600, False), (0, False), (179, False), (180, True), (86400, True)],
)
def test_heartbeat_grace_boundary(late_s, breached):
    result = invariants.heartbeat_overdue(_heartbeat(late_s), NOW, CONFIG)
    assert bool(result) is breached
    if breached:
        assert result[0].severity == "CRITICAL"
        assert result[0].observed == f"{late_s}s late"
        assert result[0].limit == "180s past next_due"


def test_absent_heartbeat_is_a_critical_missing_channel():
    (breach,) = invariants.heartbeat_overdue(None, NOW, CONFIG)
    assert breach.severity == "CRITICAL"
    assert breach.name == "heartbeat_overdue"
    assert breach.observed == "no heartbeat channel"
    assert "risk db is missing or unreadable" in breach.detail


def test_naive_next_due_is_read_as_utc_not_ignored():
    beat = _heartbeat(200)
    beat["next_due"] = datetime.fromisoformat(beat["next_due"]).replace(tzinfo=None).isoformat()
    (breach,) = invariants.heartbeat_overdue(beat, NOW, CONFIG)
    assert breach.observed == "200s late"


# -- open order count -------------------------------------------------------


@pytest.mark.parametrize("count, breached", [(0, False), (14, False), (15, True), (40, True)])
def test_open_order_count_boundary(count, breached):
    result = invariants.open_order_count(_orders(count), CONFIG)
    assert bool(result) is breached
    if breached:
        assert result[0].severity == "CRITICAL"
        assert result[0].observed == str(count)
        assert result[0].limit == "15"


# -- gross exposure ---------------------------------------------------------


@pytest.mark.parametrize(
    "values, breached",
    [
        ((), False),
        (("9499.99",), False),
        (("9500",), True),
        (("5000", "4500"), True),
        (("5000", "4499.99"), False),
    ],
)
def test_gross_exposure_boundary(values, breached):
    result = invariants.gross_exposure(_positions(*values), CONFIG)
    assert bool(result) is breached
    if breached:
        assert result[0].severity == "CRITICAL"
        assert result[0].limit == "9500"


def test_gross_exposure_counts_shorts_by_absolute_value():
    (breach,) = invariants.gross_exposure(_positions("-9500"), CONFIG)
    assert breach.observed == "9500"


# -- daily P&L floor --------------------------------------------------------


@pytest.mark.parametrize(
    "equity, breached",
    [("10500", False), ("9700.01", False), ("9700", True), ("9000", True)],
)
def test_daily_pnl_floor_boundary(equity, breached):
    result = invariants.daily_pnl_floor(_account(equity), CONFIG)
    assert bool(result) is breached
    if breached:
        assert result[0].severity == "CRITICAL"
        assert result[0].limit == "-300"


def test_daily_pnl_without_a_prior_close_warns_rather_than_passing_silently():
    (breach,) = invariants.daily_pnl_floor(_account("10000", last_equity="0"), CONFIG)
    assert breach.severity == "WARN"
    assert breach.observed == "last_equity=0"


# -- order creation rate ----------------------------------------------------


@pytest.mark.parametrize("count, breached", [(0, False), (14, False), (15, True), (60, True)])
def test_order_creation_rate_boundary(count, breached):
    result = invariants.order_creation_rate(_orders(count), NOW, CONFIG)
    assert bool(result) is breached
    if breached:
        assert result[0].severity == "CRITICAL"
        assert result[0].observed == f"{count} in 10min"


def test_order_creation_rate_ignores_orders_outside_the_window():
    orders = _orders(14, age_s=60) + _orders(20, age_s=601)
    assert invariants.order_creation_rate(orders, NOW, CONFIG) == []


# -- equity floor -----------------------------------------------------------


@pytest.mark.parametrize(
    "equity, breached",
    [("10000", False), ("8500.01", False), ("8500", True), ("0", True)],
)
def test_equity_floor_boundary(equity, breached):
    result = invariants.equity_floor(_account(equity), CONFIG)
    assert bool(result) is breached
    if breached:
        assert result[0].severity == "CRITICAL"
        assert result[0].limit == "8500"


# -- shape / independence ---------------------------------------------------


def test_every_check_passes_a_healthy_snapshot():
    account = _account("10000")
    assert invariants.heartbeat_overdue(_heartbeat(-30), NOW, CONFIG) == []
    assert invariants.open_order_count(_orders(2), CONFIG) == []
    assert invariants.gross_exposure(_positions("3000", "2000"), CONFIG) == []
    assert invariants.daily_pnl_floor(account, CONFIG) == []
    assert invariants.order_creation_rate(_orders(3), NOW, CONFIG) == []
    assert invariants.equity_floor(account, CONFIG) == []


def test_breach_is_frozen():
    (breach,) = invariants.equity_floor(_account("100"), CONFIG)
    with pytest.raises(ValidationError):
        breach.severity = "WARN"


def test_config_rejects_unknown_keys(tmp_path):
    path = tmp_path / "watchdog.yaml"
    path.write_text("max_open_ordres: 15\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        WatchdogConfig.load(path)


def test_shipped_config_loads_and_sits_wider_than_the_engine():
    """The watchdog is the second line: if any limit here bound before the
    engine's own, the two would halt together and the operator would stop
    believing the one that means the primary controls are gone."""
    watchdog = WatchdogConfig.load(REPO / "config" / "watchdog.yaml")
    risk = yaml.safe_load((REPO / "config" / "risk.yaml").read_text(encoding="utf-8"))

    assert watchdog.max_gross_notional_usd > Decimal(risk["exposure"]["max_gross_notional_usd"])
    assert -watchdog.daily_pnl_floor_usd > Decimal(risk["breakers"]["daily_loss_usd"])
    assert watchdog.max_open_orders > risk["exposure"]["max_position_count"]
    assert watchdog.heartbeat_grace_s > risk["reconcile"]["interval_s"]

    reference = Decimal(risk["equity_reference_usd"])
    engine_halt_equity = reference * (1 - Decimal(risk["breakers"]["drawdown_halt_pct"]) / 100)
    assert watchdog.equity_floor_usd < engine_halt_equity

    # max_orders_per_10min is deliberately NOT compared: the engine's rate cap
    # is 5/min (50 per 10 minutes in theory) but only 40/day, so no single
    # ordering holds. See the comment on that key in config/watchdog.yaml.
