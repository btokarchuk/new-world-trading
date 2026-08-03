"""The scripted insanity drill: proof on demand that the safety machinery bites.

PAPER ONLY, and not by convention: the drill trips the kill switch and starves a
heartbeat, so pointing it at live would cancel real orders to make a point.

Three isolations keep it non-destructive:

- The hostile-intent flood runs on a SYNTHETIC in-session clock. A drill at 03:00
  would otherwise reject every equity intent with SESSION_CLOSED and prove
  nothing about the sizing, exposure and anti-runaway limits.
- Heartbeat starvation is asserted against a THROWAWAY supervision db. A fake
  overdue beat written into the live one would make the real watchdog cancel real
  orders. nwt_risk must not import nwt_watchdog, so this scenario asserts the
  observable DB state a watchdog reads; that the watchdog PROCESS reacts to it is
  proven by the containerized drill (`make drill` with the stack up).
- The kill-switch scenario snapshots state and arming intent and restores both
  through recorded operator transitions.

The result is written to the alerts outbox under category "drill": the
live-arming checklist reads drill events as evidence, and a drill that was not
logged did not happen.
"""

import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Protocol

from pydantic import BaseModel

from nwt_contracts import (
    AssetClass,
    OrderIntent,
    OrderRef,
    PortfolioView,
    PositionView,
    RiskContext,
    SessionInfo,
    Side,
    TradingState,
)

from .alerts import AlertOutbox
from .checks import default_checks
from .checks.base import PreTradeCheck
from .config import RiskConfig
from .context import GovernorContext, QuoteView, RecentOrder
from .governor import RiskGovernor
from .reasons import ReasonCode
from .state import TradingStateMachine
from .supervision import SupervisionStore

LIVE_REFUSED = "the insanity drill trips the kill switch and starves a heartbeat — paper only"

#: The watchdog's real grace lives in config/watchdog.yaml, which this package
#: must not read; the drill states the grace it asserted against in its steps so
#: a mismatch is visible in the evidence rather than silently assumed away.
DEFAULT_HEARTBEAT_GRACE_S = 120

_FLOOD = "hostile-intent-flood"
_STARVATION = "heartbeat-starvation"
_KILL = "kill-switch"
_ACKS = "resume-requires-acks"

_DRILL_KILL_BREAKER = "drill_kill_switch"
_DRILL_ACK_BREAKER = "drill_resume_ack"

# Tue 2026-01-06 10:30 ET: inside RTH, before the entry cutoff, inside the
# crypto supervision window. Fixed so the flood replays identically at any hour.
_CLOCK = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)
_SYMBOL = "DRILL"
_CRYPTO = "BTC/USD"
_PRICE = Decimal("100")
_ADV = Decimal("1000000")


class LiveDrillRefused(RuntimeError):
    pass


class KillSwitch(Protocol):
    def cancel_all(self) -> None: ...


class DrillResult(BaseModel, frozen=True):
    scenario: str
    passed: bool
    steps: tuple[str, ...]
    failures: tuple[str, ...]
    started_at: datetime
    ended_at: datetime


class _Log:
    """Every assertion lands in `steps` carrying its own PASS/FAIL marker so the
    CLI can print the transcript verbatim and the outbox row is self-describing."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.failures: list[str] = []

    def check(self, scenario: str, ok: bool, message: str) -> bool:
        self.steps.append(f"{'PASS' if ok else 'FAIL'} [{scenario}] {message}")
        if not ok:
            self.failures.append(f"[{scenario}] {message}")
        return ok


class _HostileCase(BaseModel, frozen=True):
    label: str
    intent: OrderIntent
    ctx: GovernorContext
    expect: ReasonCode


def run_insanity_drill(
    *,
    machine: TradingStateMachine,
    outbox: AlertOutbox,
    config: RiskConfig,
    broker: KillSwitch,
    env: str,
    now_fn: Callable[[], datetime],
    checks: list[PreTradeCheck] | None = None,
    supervision_db: Path | None = None,
    heartbeat_grace_s: int = DEFAULT_HEARTBEAT_GRACE_S,
) -> DrillResult:
    """Run every insanity scenario and record the transcript in the outbox."""
    started_at = now_fn()
    if env != "paper":
        outbox.raise_alert(
            "CRITICAL",
            "drill",
            f"insanity drill REFUSED on env {env!r}",
            {"env": env, "reason": LIVE_REFUSED},
        )
        raise LiveDrillRefused(f"{LIVE_REFUSED} (env={env!r})")

    log = _Log()
    _flood_scenario(log, config, checks if checks is not None else default_checks())
    _starvation_scenario(log, now_fn, heartbeat_grace_s, supervision_db)
    _kill_switch_scenario(log, machine, broker)
    _resume_acks_scenario(log, machine)

    result = DrillResult(
        scenario="insanity",
        passed=not log.failures,
        steps=tuple(log.steps),
        failures=tuple(log.failures),
        started_at=started_at,
        ended_at=now_fn(),
    )
    outbox.raise_alert(
        "INFO" if result.passed else "CRITICAL",
        "drill",
        f"insanity drill {'PASSED' if result.passed else 'FAILED'}:"
        f" {len(result.steps)} steps, {len(result.failures)} failures",
        result.model_dump(mode="json"),
    )
    return result


# -- scenario 1: hostile-intent flood ---------------------------------------


def _flood_scenario(log: _Log, cfg: RiskConfig, checks: list[PreTradeCheck]) -> None:
    sleeve_id = next(iter(cfg.sleeves), None)
    if sleeve_id is None:
        log.check(
            _FLOOD,
            False,
            "risk config declares no sleeves: sleeve-scoped hostile intents cannot be built",
        )
        return
    # ACTIVE on purpose: the drill proves the LIMITS reject these, not that a
    # halted state happens to be blocking everything today.
    governor = RiskGovernor(checks, cfg, state_fn=lambda: TradingState.ACTIVE)
    approved = 0
    cases = _hostile_cases(cfg, sleeve_id)
    for case in cases:
        outcome = governor.review([case.intent], case.ctx)
        approved += len(outcome.approved)
        verdict = outcome.verdicts[0]
        reasons = [r.value for r in verdict.reject_reasons]
        log.check(
            _FLOOD,
            verdict.decision == "reject" and case.expect in verdict.reject_reasons,
            f"{case.label}: want {case.expect.value}, got {verdict.decision} {reasons}",
        )
    log.check(
        _FLOOD,
        approved == 0,
        f"{len(cases)} hostile intents against config {cfg.config_hash[:12]}:"
        f" {approved} approved (want 0)",
    )


def _hostile_cases(cfg: RiskConfig, sleeve_id: str) -> tuple[_HostileCase, ...]:
    order, exposure = cfg.order, cfg.exposure
    stale, rate = cfg.staleness, cfg.rate
    budget = cfg.sleeves[sleeve_id]

    def case(label: str, intent: OrderIntent, ctx: GovernorContext, expect: ReasonCode):
        return _HostileCase(label=label, intent=intent, ctx=ctx, expect=expect)

    dust_price = order.min_notional_usd / 2
    gross_positions: list[PositionView] = []
    remaining = exposure.max_gross_notional_usd
    while remaining > 0:
        chunk = min(exposure.max_symbol_notional_usd, remaining)
        gross_positions.append(
            PositionView(symbol=f"DRILLG{len(gross_positions)}", qty=Decimal("1"), avg_cost=chunk)
        )
        remaining -= chunk
    count_positions = tuple(
        PositionView(symbol=f"DRILLC{i}", qty=Decimal("1"), avg_cost=Decimal("1"))
        for i in range(exposure.max_position_count)
    )
    burst = tuple(
        RecentOrder(
            ts=_CLOCK - timedelta(seconds=5 + i),
            symbol=f"DRILLR{i}",
            side=Side.BUY,
            sleeve_id=sleeve_id,
            is_entry=True,
        )
        for i in range(rate.global_per_min)
    )

    return (
        case(
            "quote older than the staleness budget",
            _intent(sleeve_id, "h-stale-quote"),
            _ctx(cfg, sleeve_id, quote_ts=_CLOCK - timedelta(seconds=stale.max_quote_age_s + 60)),
            ReasonCode.STALE_QUOTE,
        ),
        case(
            "books unverified since before the reconcile budget",
            _intent(sleeve_id, "h-stale-reconcile"),
            _ctx(cfg, sleeve_id, reconcile_age=float(stale.max_reconcile_age_s + 60)),
            ReasonCode.STALE_RECONCILE,
        ),
        case(
            "clock skewed past tolerance",
            _intent(sleeve_id, "h-clock-skew"),
            _ctx(cfg, sleeve_id, clock_skew=float(stale.max_clock_skew_s + 5)),
            ReasonCode.CLOCK_SKEW,
        ),
        case(
            "equity entry with the exchange shut",
            _intent(sleeve_id, "h-session"),
            _ctx(cfg, sleeve_id, sessions=()),
            ReasonCode.SESSION_CLOSED,
        ),
        case(
            "crypto entry outside the supervision window",
            _intent(
                sleeve_id,
                "h-crypto-window",
                symbol=_CRYPTO,
                asset_class=AssetClass.CRYPTO,
                qty=None,
                notional=order.min_notional_usd + 1,
                limit=None,
                now=_CLOCK - timedelta(hours=8),
            ),
            _ctx(cfg, sleeve_id, symbol=_CRYPTO, now=_CLOCK - timedelta(hours=8)),
            ReasonCode.CRYPTO_WINDOW,
        ),
        case(
            "symbol outside the vetted universe",
            _intent(sleeve_id, "h-universe", symbol="NOTVETTED"),
            _ctx(cfg, sleeve_id, symbol="NOTVETTED", in_universe=False),
            ReasonCode.NOT_IN_UNIVERSE,
        ),
        case(
            "sub-floor share price",
            _intent(
                sleeve_id,
                "h-penny",
                qty=(order.min_notional_usd + 1).to_integral_value(),
                limit=Decimal("1"),
            ),
            _ctx(cfg, sleeve_id, price=Decimal("1")),
            ReasonCode.PRICE_TOO_LOW,
        ),
        case(
            "limit price far outside the collar",
            _intent(sleeve_id, "h-collar", limit=_PRICE * 3 / 2),
            _ctx(cfg, sleeve_id),
            ReasonCode.PRICE_COLLAR_BREACH,
        ),
        case(
            "dust order under the notional floor",
            _intent(
                sleeve_id,
                "h-dust",
                symbol=_CRYPTO,
                asset_class=AssetClass.CRYPTO,
                qty=None,
                notional=dust_price,
                limit=None,
            ),
            _ctx(cfg, sleeve_id, symbol=_CRYPTO),
            ReasonCode.MIN_NOTIONAL,
        ),
        case(
            "naked short of an unheld symbol",
            _intent(sleeve_id, "h-short", side=Side.SELL, qty=Decimal("5")),
            _ctx(cfg, sleeve_id),
            ReasonCode.PHANTOM_POSITION,
        ),
        case(
            "same symbol/side/sleeve inside the duplicate window",
            _intent(sleeve_id, "h-duplicate"),
            _ctx(
                cfg,
                sleeve_id,
                recent=(
                    RecentOrder(
                        ts=_CLOCK - timedelta(seconds=5),
                        symbol=_SYMBOL,
                        side=Side.BUY,
                        sleeve_id=sleeve_id,
                        is_entry=True,
                    ),
                ),
            ),
            ReasonCode.DUPLICATE_WINDOW,
        ),
        case(
            "second entry while one is already working",
            _intent(sleeve_id, "h-open-entry"),
            _ctx(
                cfg,
                sleeve_id,
                open_orders=(
                    OrderRef(
                        client_order_id="drill-working",
                        symbol=_SYMBOL,
                        side=Side.BUY,
                        qty=Decimal("1"),
                        limit_price=_PRICE,
                        submitted_at=_CLOCK - timedelta(seconds=600),
                    ),
                ),
            ),
            ReasonCode.OPEN_ENTRY_EXISTS,
        ),
        case(
            "runaway loop past the per-minute rate limit",
            _intent(sleeve_id, "h-rate"),
            _ctx(cfg, sleeve_id, recent=burst),
            ReasonCode.RATE_GLOBAL_MIN,
        ),
        case(
            "symbol exposure already at the cap",
            _intent(sleeve_id, "h-symbol-cap"),
            _ctx(
                cfg,
                sleeve_id,
                positions=(
                    PositionView(
                        symbol=_SYMBOL,
                        qty=exposure.max_symbol_notional_usd / _PRICE,
                        avg_cost=_PRICE,
                    ),
                ),
            ),
            ReasonCode.SYMBOL_EXPOSURE_CAP,
        ),
        case(
            "gross exposure already at the cap",
            _intent(sleeve_id, "h-gross-cap"),
            _ctx(cfg, sleeve_id, positions=tuple(gross_positions)),
            ReasonCode.GROSS_EXPOSURE_CAP,
        ),
        case(
            "position count already at the cap",
            _intent(sleeve_id, "h-count-cap"),
            _ctx(cfg, sleeve_id, positions=count_positions),
            ReasonCode.POSITION_COUNT_CAP,
        ),
        case(
            "sleeve budget already spent",
            _intent(sleeve_id, "h-sleeve-budget"),
            _ctx(
                cfg,
                sleeve_id,
                sleeve_positions=(
                    PositionView(
                        symbol="DRILLB", qty=Decimal("1"), avg_cost=budget.budget_usd
                    ),
                ),
            ),
            ReasonCode.SLEEVE_BUDGET_EXCEEDED,
        ),
    )


def _intent(
    sleeve_id: str,
    intent_id: str,
    *,
    symbol: str = _SYMBOL,
    asset_class: AssetClass = AssetClass.ETF,
    side: Side = Side.BUY,
    qty: Decimal | None = Decimal("1"),
    notional: Decimal | None = None,
    limit: Decimal | None = _PRICE,
    now: datetime = _CLOCK,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        sleeve_id=sleeve_id,
        strategy="insanity_drill",
        symbol=symbol,
        asset_class=asset_class,
        side=side,
        qty=qty,
        notional=notional,
        limit_price=limit,
        as_of=now,
        created_at=now,
        provenance="operator",
    )


def _ctx(
    cfg: RiskConfig,
    sleeve_id: str,
    *,
    now: datetime = _CLOCK,
    symbol: str = _SYMBOL,
    price: Decimal = _PRICE,
    quote_ts: datetime | None = None,
    in_universe: bool = True,
    positions: tuple[PositionView, ...] = (),
    sleeve_positions: tuple[PositionView, ...] = (),
    sessions: tuple[SessionInfo, ...] | None = None,
    reconcile_age: float = 10.0,
    clock_skew: float = 0.0,
    recent: tuple[RecentOrder, ...] = (),
    open_orders: tuple[OrderRef, ...] = (),
) -> GovernorContext:
    equity = cfg.equity_reference_usd
    base = RiskContext(
        ts=now,
        mode="paper",
        trading_state=TradingState.ACTIVE,
        account=PortfolioView(
            scope="account", ts=now, cash=equity, equity=equity, positions=positions
        ),
        sleeves=(
            PortfolioView(
                scope=sleeve_id,
                ts=now,
                cash=Decimal("0"),
                equity=Decimal("0"),
                positions=sleeve_positions,
            ),
        ),
        open_orders=open_orders,
        sessions=(SessionInfo(calendar="XNYS", is_open=True),) if sessions is None else sessions,
        last_reconcile_age_s=reconcile_age,
    )
    return GovernorContext(
        base=base,
        quotes={symbol: QuoteView(symbol=symbol, ts=quote_ts or now, last=price)},
        recent_orders=recent,
        open_orders=open_orders,
        adv_by_symbol={symbol: _ADV} if in_universe else {},
        clock_skew_s=clock_skew,
    )


# -- scenario 2: heartbeat starvation ---------------------------------------


def _starvation_scenario(
    log: _Log,
    now_fn: Callable[[], datetime],
    grace_s: int,
    supervision_db: Path | None,
) -> None:
    with ExitStack() as stack:
        if supervision_db is None:
            tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="nwt-drill-"))
            path = Path(tmp) / "supervision.db"
        else:
            path = supervision_db
        store = SupervisionStore(path)
        stack.callback(store.conn.close)
        now = now_fn()

        overdue_s = grace_s + 60
        store.beat(
            now - timedelta(seconds=overdue_s + 30),
            now - timedelta(seconds=overdue_s),
            "cycle",
            "drill: promise the engine did not keep",
        )
        starved = store.last_beat()
        written = log.check(
            _STARVATION, starved is not None, "starved heartbeat written and read back"
        )
        if not written:
            return
        breach_s = starved.overdue_by(now).total_seconds()
        log.check(
            _STARVATION,
            breach_s > grace_s,
            f"seq {starved.seq} overdue by {breach_s:.0f}s > grace {grace_s}s"
            f" => CRITICAL breach in {path}",
        )

        store.beat(now, now + timedelta(seconds=300), "cycle", "drill: promise kept")
        healthy = store.last_beat()
        log.check(
            _STARVATION,
            healthy is not None and healthy.overdue_by(now).total_seconds() <= 0,
            "a kept promise is NOT a breach (a supervisor that alarms on everything"
            " is no supervisor)",
        )

        command_id = store.issue(now, "HALT", "drill", "watchdog command path")
        pending = store.pending_commands()
        log.check(
            _STARVATION,
            [c.command_id for c in pending] == [command_id]
            and pending[0].command == "HALT",
            f"watchdog HALT command readable by the engine (id {command_id})",
        )
        store.consume(command_id)
        log.check(
            _STARVATION, store.pending_commands() == [], "consumed command stops being pending"
        )
        log.check(
            _STARVATION,
            True,
            "asserted against a throwaway db: that the watchdog PROCESS reacts is"
            " proven by the containerized drill, not here",
        )


# -- scenarios 3 and 4: the state machine under a real kill -----------------


def _kill_switch_scenario(log: _Log, machine: TradingStateMachine, broker: KillSwitch) -> None:
    record = machine.current()
    prior_state, prior_armed = record.state, machine.armed()
    if not _no_outstanding_latches(log, _KILL, record.latches):
        return

    try:
        broker.cancel_all()
        log.check(_KILL, True, "broker cancel_all accepted")
    except Exception as exc:
        log.check(_KILL, False, f"broker cancel_all raised {type(exc).__name__}: {exc}")

    machine.trip(
        _DRILL_KILL_BREAKER, TradingState.HALTED, ReasonCode.KILL_SWITCH, "insanity drill"
    )
    log.check(
        _KILL,
        machine.state() is TradingState.HALTED,
        f"state after kill: {machine.state().value} (want HALTED)",
    )
    latch = next(
        (
            latch
            for latch in machine.current().latches
            if latch.breaker == _DRILL_KILL_BREAKER and not latch.acked
        ),
        None,
    )
    log.check(
        _KILL,
        latch is not None and latch.reason is ReasonCode.KILL_SWITCH,
        f"kill latched: {latch.reason.value if latch else 'NO LATCH'}",
    )
    _restore(log, _KILL, machine, prior_state, prior_armed)


def _resume_acks_scenario(log: _Log, machine: TradingStateMachine) -> None:
    record = machine.current()
    prior_state, prior_armed, mode = record.state, machine.armed(), record.mode
    if not _no_outstanding_latches(log, _ACKS, record.latches):
        return

    machine.trip(
        _DRILL_ACK_BREAKER, TradingState.HALTED, ReasonCode.OPERATOR, "insanity drill: resume gate"
    )
    unacked = machine.request_transition(TradingState.ACTIVE, "drill", f"RESUME {mode}", [])
    log.check(
        _ACKS,
        not unacked.ok and machine.state() is TradingState.HALTED,
        f"resume without acking the latch refused: {unacked.error!r}",
    )
    latch_ids = [latch.latch_id for latch in machine.current().latches if not latch.acked]
    mistyped = machine.request_transition(TradingState.ACTIVE, "drill", "yes go", latch_ids)
    log.check(
        _ACKS,
        not mistyped.ok and machine.state() is TradingState.HALTED,
        f"resume with a mistyped confirmation refused: {mistyped.error!r}",
    )
    _restore(log, _ACKS, machine, prior_state, prior_armed)


def _no_outstanding_latches(log: _Log, scenario: str, latches: tuple) -> bool:
    outstanding = [latch.latch_id for latch in latches if not latch.acked]
    if not outstanding:
        return True
    # Restoring state acks every un-acked latch, so drilling now would silently
    # clear breaker latches a human has not reviewed. Refuse instead.
    log.check(
        scenario,
        False,
        f"un-acked latches outstanding {outstanding}: the drill will not ack an"
        " operator's latches — review them with `nwt-risk resume`, then re-run",
    )
    return False


def _restore(
    log: _Log,
    scenario: str,
    machine: TradingStateMachine,
    prior_state: TradingState,
    prior_armed: bool,
) -> None:
    mode = machine.current().mode
    confirmation = f"RESUME {mode}"
    latch_ids = [latch.latch_id for latch in machine.current().latches if not latch.acked]
    # Latches are only acked on the way UP, so restoring to HALTED still has to
    # climb one rung. REDUCING is the lowest rung that runs the ack path, and it
    # admits reduce-only flow — never a momentary ACTIVE with an engine running.
    hop = TradingState.REDUCING if prior_state is TradingState.HALTED else prior_state
    result = machine.request_transition(hop, "drill", confirmation, latch_ids)
    if result.ok and hop is not prior_state:
        result = machine.request_transition(prior_state, "drill", confirmation, [])
    log.check(
        scenario,
        result.ok and machine.state() is prior_state,
        f"prior state {prior_state.value} restored by recorded operator transition"
        f" (now {machine.state().value}, error {result.error!r})",
    )
    if machine.armed() == prior_armed:
        log.check(scenario, True, f"arming intent preserved (armed={prior_armed})")
    else:
        log.check(
            scenario,
            False,
            "arming intent CLEARED by the drill (was armed while not ACTIVE):"
            " re-arm with `nwt-risk resume --to ACTIVE`",
        )
