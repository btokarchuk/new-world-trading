"""Pure invariant checks: plain dicts in, Breach list out, no I/O.

Severity has two levels and they mean different things to act():

  CRITICAL — the watchdog cancels orders and files a HALT. Every numeric limit
             here is CRITICAL by construction: the thresholds sit well outside
             the engine's own, so reaching one is never routine, and "warn and
             hope" is exactly the failure mode this process exists to prevent.
  WARN     — the watchdog can see that it CANNOT see. No cancel (there is no
             evidence of harm to stop) but the blind spot is alerted rather
             than swallowed, which is the only honest option.

Boundary convention: at the limit is already a breach — `>=` for caps, `<=`
for floors. The watchdog's numbers are wide enough that landing exactly on one
means something has already gone wrong.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from .config import WatchdogConfig

RATE_WINDOW = timedelta(minutes=10)

Severity = Literal["WARN", "CRITICAL"]


class Breach(BaseModel, frozen=True):
    name: str
    severity: Severity
    detail: str
    observed: str
    limit: str


def _dec(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _ts(value) -> datetime:
    """Timestamps cross the sqlite/JSON boundary as ISO strings. A naive value
    is read as UTC: the engine writes UTC, and refusing to compare would blind
    the watchdog at exactly the moment the clock convention broke."""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def heartbeat_overdue(
    heartbeat: dict | None, now: datetime, config: WatchdogConfig
) -> list[Breach]:
    """The heartbeat is a promise (`next_due`), not a pulse, so this needs no
    trading calendar: an overnight sleep and a 30s poll look identical here."""
    limit = f"{config.heartbeat_grace_s}s past next_due"
    if heartbeat is None:
        return [
            Breach(
                name="heartbeat_overdue",
                severity="CRITICAL",
                detail=(
                    "no heartbeat channel: the risk db is missing or unreadable, or the"
                    " engine has never written a beat"
                ),
                observed="no heartbeat channel",
                limit=limit,
            )
        ]
    next_due = _ts(heartbeat["next_due"])
    late_s = int((now - next_due).total_seconds())
    if late_s < config.heartbeat_grace_s:
        return []
    return [
        Breach(
            name="heartbeat_overdue",
            severity="CRITICAL",
            detail=(
                f"engine promised to return by {next_due.isoformat()}"
                f" (seq={heartbeat.get('seq')}, phase={heartbeat.get('phase')})"
            ),
            observed=f"{late_s}s late",
            limit=limit,
        )
    ]


def open_order_count(open_orders: list[dict], config: WatchdogConfig) -> list[Breach]:
    count = len(open_orders)
    if count < config.max_open_orders:
        return []
    return [
        Breach(
            name="open_order_count",
            severity="CRITICAL",
            detail="open orders piling up at the broker: engine rate limits are not holding",
            observed=str(count),
            limit=str(config.max_open_orders),
        )
    ]


def gross_exposure(positions: list[dict], config: WatchdogConfig) -> list[Breach]:
    gross = sum((abs(_dec(p.get("market_value"))) for p in positions), Decimal("0"))
    if gross < config.max_gross_notional_usd:
        return []
    return [
        Breach(
            name="gross_exposure",
            severity="CRITICAL",
            detail=f"gross notional across {len(positions)} positions exceeds the account cap",
            observed=f"{gross}",
            limit=f"{config.max_gross_notional_usd}",
        )
    ]


def daily_pnl_floor(account: dict, config: WatchdogConfig) -> list[Breach]:
    last_equity = _dec(account.get("last_equity"))
    equity = _dec(account.get("equity"))
    if last_equity <= 0:
        # No prior close to difference against (fresh or reset account). The
        # check is blind, not passing — say so instead of returning [].
        return [
            Breach(
                name="daily_pnl_floor",
                severity="WARN",
                detail="daily P&L unknowable: broker reported no prior-session equity",
                observed=f"last_equity={last_equity}",
                limit=f"{config.daily_pnl_floor_usd}",
            )
        ]
    pnl = equity - last_equity
    if pnl > config.daily_pnl_floor_usd:
        return []
    return [
        Breach(
            name="daily_pnl_floor",
            severity="CRITICAL",
            detail=f"session P&L below the floor (equity {equity} vs prior close {last_equity})",
            observed=f"{pnl}",
            limit=f"{config.daily_pnl_floor_usd}",
        )
    ]


def order_creation_rate(
    orders: list[dict], now: datetime, config: WatchdogConfig
) -> list[Breach]:
    """`orders` is whatever the broker returned for the lookback; the window is
    re-applied here because Alpaca's `after` filter is not exact."""
    cutoff = now - RATE_WINDOW
    recent = [o for o in orders if _ts(o["created_at"]) >= cutoff]
    if len(recent) < config.max_orders_per_10min:
        return []
    return [
        Breach(
            name="order_creation_rate",
            severity="CRITICAL",
            detail="runaway order submission: the engine is spraying orders at the broker",
            observed=f"{len(recent)} in {int(RATE_WINDOW.total_seconds() // 60)}min",
            limit=str(config.max_orders_per_10min),
        )
    ]


def unprotected_positions(
    positions: list[dict],
    open_orders: list[dict],
    config: WatchdogConfig,
) -> list[Breach]:
    """Every share should sit behind a resting sell stop — minus allowances.

    Computed from broker state alone: the broker sees ONE position per symbol
    while sleeves see lots, so the per-sleeve view cannot be derived here
    without reading the engine's database, which this package must never do.
    The bridge is `protection_allowances` in config/watchdog.yaml — a static,
    human-maintained map of shares deliberately unprotected (the control
    sleeve's benchmark lot; crypto, where Alpaca offers no stop type worth
    trusting). Static config preserves independence; the cost is that a stale
    allowance makes this check quietly toothless, so a companion breach fires
    when an allowance exceeds the position it excuses.

    WARN today, by design (calibration period — see the ship-order decision in
    docs/design/protective-stops.md §8). Promote via protection_critical once
    the arming path has baked.
    """
    if not config.protection_check:
        return []
    severity = "CRITICAL" if config.protection_critical else "WARN"
    breaches: list[Breach] = []
    covered: dict[str, Decimal] = {}
    for order in open_orders:
        if order.get("side") == "sell" and order.get("type") == "stop":
            symbol = order.get("symbol", "")
            covered[symbol] = covered.get(symbol, Decimal("0")) + _dec(order.get("qty"))
    for position in positions:
        symbol = position.get("symbol", "")
        if position.get("asset_class") == "crypto":
            # Alpaca offers no stop type worth trusting for crypto (design §7);
            # crypto exposure is bounded by sleeve size instead.
            continue
        qty = _dec(position.get("qty"))
        allowance = _dec(config.protection_allowances.get(symbol, "0"))
        if allowance > qty:
            breaches.append(
                Breach(
                    name="stale_protection_allowance",
                    severity="WARN",
                    detail=(
                        f"{symbol}: allowance {allowance} exceeds the position {qty} it"
                        " excuses — config/watchdog.yaml is stale and this symbol's"
                        " coverage check is toothless until it is corrected"
                    ),
                    observed=f"allowance {allowance}",
                    limit=f"position {qty}",
                )
            )
        uncovered = qty - covered.get(symbol, Decimal("0")) - allowance
        if uncovered > 0:
            breaches.append(
                Breach(
                    name="unprotected_position",
                    severity=severity,
                    detail=(
                        f"{symbol}: {uncovered} share(s) with no resting sell stop —"
                        " if the engine dies, nothing can exit this position"
                    ),
                    observed=f"{uncovered} unprotected of {qty}",
                    limit="every share stopped or allowed-for",
                )
            )
    return breaches


def equity_floor(account: dict, config: WatchdogConfig) -> list[Breach]:
    equity = _dec(account.get("equity"))
    if equity > config.equity_floor_usd:
        return []
    return [
        Breach(
            name="equity_floor",
            severity="CRITICAL",
            detail="account equity below the hard floor: stop trading and post-mortem",
            observed=f"{equity}",
            limit=f"{config.equity_floor_usd}",
        )
    ]
