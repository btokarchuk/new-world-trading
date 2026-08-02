"""Chaos suite: the Knight-prevention proof, end to end through REAL components.

Every scenario wires AlpacaHttpBroker to the stateful FakeAlpaca and drives the
real TradingStateMachine / CircuitBreakers / ReconcileEngine / RiskGovernor
(default checks, shipping config/risk.yaml). No component is mocked — only the
broker's far side is simulated.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from fake_alpaca import FakeAlpaca

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
from nwt_engine.broker.alpaca import AlpacaHttpBroker
from nwt_engine.domain import OrderState, OrderTicket
from nwt_risk import (
    GovernorContext,
    QuoteView,
    ReasonCode,
    RecentOrder,
    RiskGovernor,
    SymbolCooldown,
)
from nwt_risk.breakers.monitors import BreakerEvent, CircuitBreakers
from nwt_risk.checks import default_checks
from nwt_risk.config import RiskConfig
from nwt_risk.reconcile import ExpectedState, ReconcileEngine
from nwt_risk.state import TradingStateMachine

BASE = "https://paper-api.alpaca.markets"
NOW = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)  # Tue 10:30 ET: XNYS open, crypto window open
CFG = RiskConfig.load(Path(__file__).resolve().parents[2] / "config" / "risk.yaml")


class FakeClock:
    def __init__(self, start: datetime = NOW) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, delta: timedelta) -> None:
        self._t += delta


def _broker(fake: FakeAlpaca) -> AlpacaHttpBroker:
    return AlpacaHttpBroker(BASE, "key", "secret", client=fake.client())


def _active_machine(db, clock: FakeClock) -> TradingStateMachine:
    machine = TradingStateMachine(db, "paper", clock.now)
    machine.on_startup()
    ids = [latch.latch_id for latch in machine.current().latches if not latch.acked]
    assert machine.request_transition(
        TradingState.ACTIVE, "chaos-op", "RESUME paper 10000", ids
    ).ok
    return machine


def _unacked(machine: TradingStateMachine) -> list[tuple[str, ReasonCode]]:
    return [
        (latch.breaker, latch.reason)
        for latch in machine.current().latches
        if not latch.acked
    ]


def _quote(symbol: str, last: str, ts: datetime = NOW, bid=None, ask=None) -> QuoteView:
    return QuoteView(
        symbol=symbol,
        ts=ts,
        last=D(last),
        bid=None if bid is None else D(bid),
        ask=None if ask is None else D(ask),
    )


def _intent(
    intent_id: str,
    symbol: str,
    *,
    side: Side = Side.BUY,
    qty: D | None = D("3"),
    notional: D | None = None,
    limit_price: D | None = D("100"),
    sleeve_id: str = "control",
    asset_class: AssetClass = AssetClass.EQUITY,
    reduces_position: bool = False,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        sleeve_id=sleeve_id,
        strategy="chaos",
        symbol=symbol,
        asset_class=asset_class,
        side=side,
        qty=qty,
        notional=notional,
        limit_price=limit_price,
        as_of=NOW,
        created_at=NOW,
        reduces_position=reduces_position,
        provenance="llm",
    )


def _ctx(
    state: TradingState,
    quotes: dict[str, QuoteView],
    adv: dict[str, D],
    *,
    account_positions=(),
    control_positions=(),
    crypto_positions=(),
    recent=(),
    open_orders=(),
    cooldowns=(),
) -> GovernorContext:
    base = RiskContext(
        ts=NOW,
        mode="paper",
        trading_state=state,
        account=PortfolioView(
            scope="account",
            ts=NOW,
            cash=D("7000"),
            equity=D("10000"),
            positions=tuple(account_positions),
        ),
        sleeves=(
            PortfolioView(
                scope="control",
                ts=NOW,
                cash=D("2500"),
                equity=D("2500"),
                positions=tuple(control_positions),
            ),
            PortfolioView(
                scope="crypto_momo",
                ts=NOW,
                cash=D("0"),
                equity=D("770"),
                positions=tuple(crypto_positions),
            ),
        ),
        open_orders=tuple(open_orders),
        sessions=(SessionInfo(calendar="XNYS", is_open=True),),
        last_reconcile_age_s=10.0,
    )
    return GovernorContext(
        base=base,
        quotes=quotes,
        recent_orders=tuple(recent),
        open_orders=tuple(open_orders),
        cooldowns=tuple(cooldowns),
        adv_by_symbol=adv,
    )


def _drive_mismatch_halt(db, clock: FakeClock):
    """Scenario-5 runtime glue: fill, clean reconcile, quiet tamper, HALT."""
    fake = FakeAlpaca(cash="10000")
    broker = _broker(fake)
    machine = _active_machine(db, clock)
    audit: list[tuple[str, dict]] = []
    engine = ReconcileEngine(
        broker, CFG.reconcile, clock.now, lambda kind, payload: audit.append((kind, payload))
    )

    ack = broker.submit(
        OrderTicket(
            client_order_id="c-aapl-1",
            symbol="AAPL",
            side=Side.BUY,
            qty=D("10"),
            limit_price=D("100"),
        )
    )
    assert ack.state is OrderState.ACKED
    fake.fill("fake-order-1", D("100"))
    expected = ExpectedState(
        cash=D("9000"), positions={"AAPL": D("10")}, open_client_order_ids=set()
    )
    assert engine.reconcile(expected).ok is True

    # Park an open order so the kill path later has something to cancel.
    broker.submit(
        OrderTicket(
            client_order_id="c-msft-1",
            symbol="MSFT",
            side=Side.BUY,
            qty=D("1"),
            limit_price=D("100"),
        )
    )
    expected = ExpectedState(
        cash=D("9000"), positions={"AAPL": D("10")}, open_client_order_ids={"c-msft-1"}
    )

    fake.positions["AAPL"]["qty"] = D("12")  # broker truth quietly mutated
    report = engine.reconcile(expected)
    if not report.ok:  # the runtime glue: unexplained reconcile => HALT
        machine.trip(
            "reconcile",
            TradingState.HALTED,
            ReasonCode.RECONCILE_MISMATCH,
            "; ".join(report.unexplained),
        )
    return fake, broker, machine, report, audit


# -- 1. timeout-but-created ---------------------------------------------------


def test_timeout_but_created_yields_exactly_one_order():
    fake = FakeAlpaca()
    broker = _broker(fake)
    ticket = OrderTicket(
        client_order_id="intent-1",
        symbol="AAPL",
        side=Side.BUY,
        qty=D("10"),
        limit_price=D("100"),
    )

    fake.timeout_but_create_next()
    ack = broker.submit(ticket)

    # The POST timed out AFTER the broker recorded it; the lookup adopted it.
    assert ack.state is OrderState.ACKED
    assert ack.client_order_id == "intent-1"
    assert [o["id"] for o in fake.orders] == ["fake-order-1"]
    assert [s.client_order_id for s in broker.get_open_orders()] == ["intent-1"]

    # Resubmitting the same intent (same client_order_id) creates no second order.
    retry = broker.submit(ticket)
    assert retry.state is OrderState.REJECTED
    assert "must be unique" in retry.reason
    assert len(fake.orders) == 1
    posts = [r for r in fake.requests if r.method == "POST"]
    assert len(posts) == 2  # one per submit call — never a blind retry


# -- 2. rejection storm -------------------------------------------------------


def test_rejection_storm_halts_and_governor_structurally_rejects(tmp_path):
    clock = FakeClock()
    db = tmp_path / "risk.db"
    fake = FakeAlpaca()
    broker = _broker(fake)
    machine = _active_machine(db, clock)
    breakers = CircuitBreakers(CFG.breakers, machine, clock.now, db)

    for i in range(5):
        fake.reject_next("insufficient buying power")
        ack = broker.submit(
            OrderTicket(
                client_order_id=f"storm-{i + 1}",
                symbol="AAPL",
                side=Side.BUY,
                qty=D("1"),
                limit_price=D("100"),
            )
        )
        assert ack.state is OrderState.REJECTED
        breakers.observe(BreakerEvent(kind="rejection", ts=clock.now()))
        clock.advance(timedelta(seconds=60))  # all five inside the 10-min window

    assert machine.state() is TradingState.HALTED
    assert ("rejection_storm", ReasonCode.REJECTION_STORM) in _unacked(machine)
    assert fake.orders == []  # nothing ever landed at the broker

    # A subsequent governor review structurally rejects everything — even an
    # otherwise-approvable entry and a reduce-only exit.
    gov = RiskGovernor(default_checks(), CFG, state_fn=machine.state)
    ctx = _ctx(
        machine.state(),
        quotes={"AAPL": _quote("AAPL", "100")},
        adv={"AAPL": D("1000000")},
        account_positions=(PositionView(symbol="AAPL", qty=D("5"), avg_cost=D("100")),),
        control_positions=(PositionView(symbol="AAPL", qty=D("5"), avg_cost=D("100")),),
    )
    out = gov.review(
        [
            _intent("post-halt-entry", "AAPL"),
            _intent(
                "post-halt-exit", "AAPL", side=Side.SELL, qty=D("1"), reduces_position=True
            ),
        ],
        ctx,
    )
    assert out.approved == ()
    for verdict in out.verdicts:
        assert verdict.decision == "reject"
        assert set(verdict.reject_reasons) == {ReasonCode.STATE_NOT_ACTIVE}
        assert any(r.check == "structural_state_gate" for r in verdict.results)


# -- 3. phantom-position sell (LLM containment) -------------------------------


def test_phantom_position_sell_rejected_and_audited(tmp_path):
    clock = FakeClock()
    machine = _active_machine(tmp_path / "risk.db", clock)
    audit: list[tuple[str, dict]] = []
    gov = RiskGovernor(
        default_checks(),
        CFG,
        state_fn=machine.state,
        audit=lambda kind, payload: audit.append((kind, payload)),
    )
    ctx = _ctx(
        machine.state(),
        quotes={"MSFT": _quote("MSFT", "100", bid="99.95", ask="100.05")},
        adv={"MSFT": D("1000000")},
    )

    out = gov.review(
        [_intent("llm-sell-1", "MSFT", side=Side.SELL, qty=D("5"), reduces_position=True)],
        ctx,
    )

    assert out.approved == ()
    verdict = out.verdicts[0]
    assert verdict.decision == "reject"
    assert verdict.reject_reasons == [ReasonCode.PHANTOM_POSITION]
    (event,) = audit
    assert event[0] == "verdict"
    assert event[1]["intent_id"] == "llm-sell-1"
    assert event[1]["decision"] == "reject"
    assert any(r["reason"] == "PHANTOM_POSITION" for r in event[1]["results"])


# -- 4. hostile-intent flood --------------------------------------------------


def test_hostile_intent_flood_all_rejected_with_exact_reasons(tmp_path):
    clock = FakeClock()
    machine = _active_machine(tmp_path / "risk.db", clock)
    gov = RiskGovernor(default_checks(), CFG, state_fn=machine.state)

    quotes = {
        "STAL": _quote("STAL", "100", ts=NOW - timedelta(seconds=31)),
        # GHST deliberately has no quote at all.
        "ALTC": _quote("ALTC", "100"),
        "PENY": _quote("PENY", "4.50"),
        "THIN": _quote("THIN", "100"),
        "COOL": _quote("COOL", "100"),
        "DUP": _quote("DUP", "100"),
        "OPEN": _quote("OPEN", "100"),
        "RATE": _quote("RATE", "100"),
        "RATD": _quote("RATD", "100"),
        "SHRT": _quote("SHRT", "100"),
        "MSFT": _quote("MSFT", "100"),
        "MINN": _quote("MINN", "10"),
        "PRCY": _quote("PRCY", "600"),
        "ROGU": _quote("ROGU", "100"),
        "COLL": _quote("COLL", "100", bid="99.95", ask="100.05"),
        "SUSP": _quote("SUSP", "100", bid="103.90", ask="104.10"),
        "XPS": _quote("XPS", "100"),
        "NEWP": _quote("NEWP", "100"),
        "BTC/USD": _quote("BTC/USD", "70000"),
    }
    adv = {symbol: D("1000000") for symbol in quotes if symbol != "ALTC"}
    adv["GHST"] = D("1000000")
    adv["THIN"] = D("50")  # 1% of ADV floors to zero shares

    # 12 filler positions arm the position-count cap; XPS sits exactly at the
    # $1000 symbol cap; BTC/USD marks to $770 — over the $750 crypto sleeve cap.
    fillers = tuple(
        PositionView(symbol=f"F{i:02d}", qty=D("1"), avg_cost=D("50")) for i in range(1, 13)
    )
    btc = PositionView(symbol="BTC/USD", qty=D("0.011"), avg_cost=D("70000"))
    account_positions = fillers + (
        PositionView(symbol="XPS", qty=D("10"), avg_cost=D("99")),
        btc,
    )
    recent = (
        RecentOrder(
            ts=NOW - timedelta(seconds=30), symbol="DUP", side=Side.BUY,
            sleeve_id="control", is_entry=True,
        ),
        RecentOrder(
            ts=NOW - timedelta(seconds=30), symbol="RATE", side=Side.SELL,
            sleeve_id="control", is_entry=False,
        ),
        RecentOrder(
            ts=NOW - timedelta(seconds=45), symbol="RATE", side=Side.SELL,
            sleeve_id="control", is_entry=False,
        ),
    ) + tuple(
        RecentOrder(
            ts=NOW - timedelta(hours=2, minutes=i), symbol="RATD", side=Side.SELL,
            sleeve_id="control", is_entry=False,
        )
        for i in range(6)
    )
    ctx = _ctx(
        machine.state(),
        quotes=quotes,
        adv=adv,
        account_positions=account_positions,
        crypto_positions=(btc,),
        recent=recent,
        open_orders=(
            OrderRef(
                client_order_id="open-1",
                symbol="OPEN",
                side=Side.BUY,
                qty=D("1"),
                limit_price=D("100"),
                submitted_at=NOW - timedelta(minutes=10),
            ),
        ),
        cooldowns=(SymbolCooldown(symbol="COOL", until=NOW + timedelta(hours=1)),),
    )

    cases = [
        (_intent("h-01", "STAL"), ReasonCode.STALE_QUOTE),
        (_intent("h-02", "GHST"), ReasonCode.STALE_QUOTE),
        (_intent("h-03", "ALTC"), ReasonCode.NOT_IN_UNIVERSE),
        (_intent("h-04", "PENY", qty=D("10"), limit_price=D("4.50")), ReasonCode.PRICE_TOO_LOW),
        (_intent("h-05", "THIN", qty=D("4")), ReasonCode.ADV_EXCEEDED),
        (_intent("h-06", "COOL"), ReasonCode.SYMBOL_COOLDOWN),
        (_intent("h-07", "DUP"), ReasonCode.DUPLICATE_WINDOW),
        (_intent("h-08", "OPEN"), ReasonCode.OPEN_ENTRY_EXISTS),
        (_intent("h-09", "RATE"), ReasonCode.RATE_SYMBOL_MIN),
        (_intent("h-10", "RATD"), ReasonCode.RATE_SYMBOL_DAY),
        (
            _intent(
                "h-11", "SHRT", side=Side.SELL, qty=D("1"),
                sleeve_id="ghost", reduces_position=True,
            ),
            ReasonCode.SHORT_FORBIDDEN,
        ),
        (
            _intent("h-12", "MSFT", side=Side.SELL, qty=D("5"), reduces_position=True),
            ReasonCode.PHANTOM_POSITION,
        ),
        (_intent("h-13", "MINN", qty=D("2"), limit_price=D("10")), ReasonCode.MIN_NOTIONAL),
        # One share priced above the $1,000 cap: clamps to zero shares -> reject.
        (_intent("h-14", "PRCY", qty=D("1"), limit_price=D("1200")), ReasonCode.ORDER_NOTIONAL_CAP),
        (_intent("h-15", "ROGU", sleeve_id="rogue"), ReasonCode.SLEEVE_BUDGET_EXCEEDED),
        (_intent("h-16", "COLL", qty=D("4"), limit_price=D("103")), ReasonCode.PRICE_COLLAR_BREACH),
        (_intent("h-17", "SUSP", qty=D("4"), limit_price=D("104")), ReasonCode.SUSPECT_QUOTE),
        (_intent("h-18", "XPS"), ReasonCode.SYMBOL_EXPOSURE_CAP),
        (
            _intent(
                "h-19", "BTC/USD", asset_class=AssetClass.CRYPTO,
                qty=None, notional=D("200"), limit_price=None, sleeve_id="crypto_momo",
            ),
            ReasonCode.CRYPTO_SLEEVE_CAP,
        ),
        (_intent("h-20", "NEWP"), ReasonCode.POSITION_COUNT_CAP),
    ]
    assert len(cases) == 20

    out = gov.review([intent for intent, _ in cases], ctx)

    assert out.approved == ()  # 100% rejected
    assert len(out.verdicts) == 20
    for (intent, expected), verdict in zip(cases, out.verdicts):
        assert verdict.intent_id == intent.intent_id
        assert verdict.decision == "reject", intent.intent_id
        assert verdict.reject_reasons[0] is expected, intent.intent_id
        assert verdict.config_hash == CFG.config_hash


# -- 5. reconcile-mismatch halt -----------------------------------------------


def test_reconcile_mismatch_halts_but_cancel_all_still_works(tmp_path):
    clock = FakeClock()
    fake, broker, machine, report, audit = _drive_mismatch_halt(tmp_path / "risk.db", clock)

    assert report.ok is False
    assert any("AAPL" in line for line in report.unexplained)
    assert machine.state() is TradingState.HALTED
    assert ("reconcile", ReasonCode.RECONCILE_MISMATCH) in _unacked(machine)
    assert audit[-1][0] == "reconcile"
    assert audit[-1][1]["ok"] is False

    # Kill-style cancel is still permitted while HALTED.
    broker.cancel_all()
    assert [o["status"] for o in fake.orders] == ["filled", "canceled"]
    assert broker.get_open_orders() == []


# -- 6. restart recovery ------------------------------------------------------


def test_restart_recovery_requires_acking_both_latches(tmp_path):
    clock = FakeClock()
    db = tmp_path / "risk.db"
    _drive_mismatch_halt(db, clock)

    machine = TradingStateMachine(db, "paper", clock.now)
    machine.on_startup()

    assert machine.state() is TradingState.HALTED
    unacked = _unacked(machine)
    assert ("reconcile", ReasonCode.RECONCILE_MISMATCH) in unacked
    assert ("startup", ReasonCode.STARTUP) in unacked
    assert len(unacked) == 2

    denied = machine.request_transition(TradingState.REDUCING, "op", "RESUME paper 10000", [])
    assert denied.ok is False
    assert machine.state() is TradingState.HALTED

    ids = [latch.latch_id for latch in machine.current().latches if not latch.acked]
    partial = machine.request_transition(
        TradingState.REDUCING, "op", "RESUME paper 10000", ids[:1]
    )
    assert partial.ok is False
    assert machine.state() is TradingState.HALTED

    granted = machine.request_transition(TradingState.REDUCING, "op", "RESUME paper 10000", ids)
    assert granted.ok is True
    assert granted.state is TradingState.REDUCING
    assert machine.state() is TradingState.REDUCING


# -- 7. external-order adoption -----------------------------------------------


def test_external_order_adopted_via_reconcile():
    clock = FakeClock()
    fake = FakeAlpaca()
    broker = _broker(fake)
    audit: list[tuple[str, dict]] = []
    engine = ReconcileEngine(
        broker, CFG.reconcile, clock.now, lambda kind, payload: audit.append((kind, payload))
    )

    fake.create_external("TSLA", side="buy", qty="1", limit_price="200")
    report = engine.reconcile(
        ExpectedState(cash=D("10000"), positions={}, open_client_order_ids=set())
    )

    assert report.external_order_ids == ("ext-1",)
    assert report.ok is True  # external is WARN-class, never a silent mismatch
    assert report.unexplained == ()
    (event,) = audit
    assert event[0] == "reconcile"
    assert event[1]["external_order_ids"] == ["ext-1"]
