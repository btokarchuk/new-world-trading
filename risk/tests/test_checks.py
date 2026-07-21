from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

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
from nwt_risk.checks import (
    CooldownCheck,
    DuplicateCheck,
    ExposureCheck,
    LongOnlyCheck,
    OrderSizeCheck,
    PriceCollarCheck,
    RateLimitCheck,
    SessionCheck,
    StalenessCheck,
    StateGateCheck,
    UniverseCheck,
    default_checks,
)
from nwt_risk.config import DuplicateRules, RiskConfig, SessionRules, SleeveBudget
from nwt_risk.context import GovernorContext, QuoteView, RecentOrder, SymbolCooldown
from nwt_risk.reasons import ReasonCode

NOW = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)  # Tue 10:30 ET, XNYS open

CFG = RiskConfig(
    sleeves={
        "s1": SleeveBudget(budget_usd=D("2500"), max_order_usd=D("500")),
        "tight": SleeveBudget(budget_usd=D("2500"), max_order_usd=D("100")),
        "big": SleeveBudget(budget_usd=D("9000"), max_order_usd=D("800")),
    },
    config_hash="cfg-test",
)


def quote(symbol="SPY", last="100", bid="99.95", ask="100.05", age_s=0) -> QuoteView:
    return QuoteView(
        symbol=symbol,
        ts=NOW - timedelta(seconds=age_s),
        last=D(last),
        bid=D(bid) if bid is not None else None,
        ask=D(ask) if ask is not None else None,
    )


def pos(symbol, qty, cost="100") -> PositionView:
    return PositionView(symbol=symbol, qty=D(qty), avg_cost=D(cost))


def oref(symbol="SPY", side=Side.BUY, qty="1", limit="100", i=0) -> OrderRef:
    return OrderRef(
        client_order_id=f"o{i}",
        symbol=symbol,
        side=side,
        qty=D(qty),
        limit_price=D(limit),
        submitted_at=NOW,
    )


def recent(symbol="SPY", side=Side.BUY, sleeve="s1", age_s=10) -> RecentOrder:
    return RecentOrder(
        ts=NOW - timedelta(seconds=age_s),
        symbol=symbol,
        side=side,
        sleeve_id=sleeve,
        is_entry=True,
    )


def make_intent(**overrides) -> OrderIntent:
    base = dict(
        intent_id="i1",
        sleeve_id="s1",
        strategy="t",
        symbol="SPY",
        asset_class=AssetClass.ETF,
        side=Side.BUY,
        qty=D("4"),
        limit_price=D("100"),
        as_of=NOW,
        created_at=NOW,
    )
    base.update(overrides)
    return OrderIntent(**base)


def crypto_intent(**overrides) -> OrderIntent:
    base = dict(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        qty=None,
        notional=D("100"),
        limit_price=None,
    )
    base.update(overrides)
    return make_intent(**base)


def make_ctx(
    *,
    now=NOW,
    state=TradingState.ACTIVE,
    quotes=None,
    account_positions=(),
    sleeve_positions=(),
    sleeve_id="s1",
    open_orders=(),
    recent_orders=(),
    cooldowns=(),
    adv=None,
    sessions=None,
    reconcile_age=10.0,
    clock_skew=0.0,
) -> GovernorContext:
    if quotes is None:
        quotes = {"SPY": quote(), "BTC/USD": quote(symbol="BTC/USD", bid=None, ask=None)}
    if adv is None:
        adv = {"SPY": D("100000"), "BTC/USD": D("100000")}
    if sessions is None:
        sessions = (SessionInfo(calendar="XNYS", is_open=True),)
    base = RiskContext(
        ts=now,
        mode="paper",
        trading_state=state,
        account=PortfolioView(
            scope="account",
            ts=now,
            cash=D("10000"),
            equity=D("10000"),
            positions=tuple(account_positions),
        ),
        sleeves=(
            PortfolioView(
                scope=sleeve_id,
                ts=now,
                cash=D("1000"),
                equity=D("2500"),
                positions=tuple(sleeve_positions),
            ),
        ),
        open_orders=tuple(open_orders),
        sessions=tuple(sessions),
        last_reconcile_age_s=reconcile_age,
    )
    return GovernorContext(
        base=base,
        quotes=quotes,
        recent_orders=tuple(recent_orders),
        open_orders=tuple(open_orders),
        cooldowns=tuple(cooldowns),
        adv_by_symbol=adv,
        clock_skew_s=clock_skew,
    )


# ------------------------------------------------------------------ state gate


@pytest.mark.parametrize(
    ("state", "reduces", "decision"),
    [
        (TradingState.ACTIVE, False, "allow"),
        (TradingState.ACTIVE, True, "allow"),
        (TradingState.REDUCING, False, "reject"),
        (TradingState.REDUCING, True, "allow"),
        (TradingState.HALTED, False, "reject"),
        (TradingState.HALTED, True, "reject"),
    ],
)
def test_state_gate(state, reduces, decision):
    res = StateGateCheck().evaluate(
        make_intent(reduces_position=reduces), make_ctx(state=state), CFG
    )
    assert res.decision == decision
    if decision == "reject":
        assert res.reason is ReasonCode.STATE_NOT_ACTIVE


# --------------------------------------------------------------------- session


@pytest.mark.parametrize(
    ("now", "is_open", "reduces", "decision", "reason"),
    [
        (NOW, True, False, "allow", None),
        (NOW, False, False, "reject", ReasonCode.SESSION_CLOSED),
        # 15:44 ET is inside the entry window; 15:45 ET is the cutoff (>=)
        (datetime(2026, 1, 6, 20, 44, tzinfo=UTC), True, False, "allow", None),
        (datetime(2026, 1, 6, 20, 45, tzinfo=UTC), True, False, "reject", ReasonCode.ENTRY_CUTOFF),
        (datetime(2026, 1, 6, 20, 45, tzinfo=UTC), True, True, "allow", None),
    ],
)
def test_session_equity(now, is_open, reduces, decision, reason):
    ctx = make_ctx(now=now, sessions=(SessionInfo(calendar="XNYS", is_open=is_open),))
    res = SessionCheck().evaluate(make_intent(reduces_position=reduces), ctx, CFG)
    assert (res.decision, res.reason) == (decision, reason)


def test_session_equity_missing_calendar_is_closed():
    res = SessionCheck().evaluate(make_intent(), make_ctx(sessions=()), CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.SESSION_CLOSED)


def test_session_equity_extended_hours_config():
    cfg = CFG.model_copy(update={"session": SessionRules(regular_hours_only=False)})
    ctx = make_ctx(sessions=(SessionInfo(calendar="XNYS", is_open=False),))
    assert SessionCheck().evaluate(make_intent(), ctx, cfg).decision == "allow"


@pytest.mark.parametrize(
    ("now", "protective", "decision"),
    [
        (datetime(2026, 1, 6, 13, 0, tzinfo=UTC), False, "allow"),  # 08:00 ET opens
        (datetime(2026, 1, 6, 12, 59, 59, tzinfo=UTC), False, "reject"),  # 07:59:59 ET
        (datetime(2026, 1, 7, 2, 59, 59, tzinfo=UTC), False, "allow"),  # 21:59:59 ET
        (datetime(2026, 1, 7, 3, 0, tzinfo=UTC), False, "reject"),  # 22:00 ET closes
        (datetime(2026, 1, 7, 3, 0, tzinfo=UTC), True, "allow"),  # protective exempt
    ],
)
def test_session_crypto_window(now, protective, decision):
    res = SessionCheck().evaluate(
        crypto_intent(is_protective=protective), make_ctx(now=now), CFG
    )
    assert res.decision == decision
    if decision == "reject":
        assert res.reason is ReasonCode.CRYPTO_WINDOW


# ------------------------------------------------------------------- staleness


@pytest.mark.parametrize(
    ("quote_age", "reconcile_age", "skew", "decision", "reason"),
    [
        (30, 10.0, 0.0, "allow", None),
        (31, 10.0, 0.0, "reject", ReasonCode.STALE_QUOTE),
        (0, 300.0, 0.0, "allow", None),
        (0, 300.1, 0.0, "reject", ReasonCode.STALE_RECONCILE),
        (0, None, 0.0, "reject", ReasonCode.STALE_RECONCILE),
        (0, 10.0, 2.0, "allow", None),
        (0, 10.0, -2.0, "allow", None),
        (0, 10.0, 2.1, "reject", ReasonCode.CLOCK_SKEW),
        (0, 10.0, -2.1, "reject", ReasonCode.CLOCK_SKEW),
    ],
)
def test_staleness_boundaries(quote_age, reconcile_age, skew, decision, reason):
    ctx = make_ctx(
        quotes={"SPY": quote(age_s=quote_age)},
        reconcile_age=reconcile_age,
        clock_skew=skew,
    )
    res = StalenessCheck().evaluate(make_intent(), ctx, CFG)
    assert (res.decision, res.reason) == (decision, reason)


def test_staleness_missing_quote_rejects():
    res = StalenessCheck().evaluate(make_intent(), make_ctx(quotes={}), CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.STALE_QUOTE)


# -------------------------------------------------------------------- universe


def test_universe_membership():
    assert UniverseCheck().evaluate(make_intent(), make_ctx(), CFG).decision == "allow"
    res = UniverseCheck().evaluate(make_intent(symbol="ZZZ"), make_ctx(), CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.NOT_IN_UNIVERSE)


@pytest.mark.parametrize(("last", "decision"), [("5.00", "allow"), ("4.99", "reject")])
def test_universe_min_price_boundary(last, decision):
    ctx = make_ctx(quotes={"SPY": quote(last=last, bid=None, ask=None)})
    res = UniverseCheck().evaluate(make_intent(limit_price=D(last)), ctx, CFG)
    assert res.decision == decision
    if decision == "reject":
        assert res.reason is ReasonCode.PRICE_TOO_LOW


def test_universe_min_price_equities_only():
    ctx = make_ctx(
        quotes={"BTC/USD": quote(symbol="BTC/USD", last="0.50", bid=None, ask=None)}
    )
    assert UniverseCheck().evaluate(crypto_intent(), ctx, CFG).decision == "allow"


def test_universe_adv_boundary():
    # 0.01 * ADV 100000 = 1000 shares
    ctx = make_ctx()
    assert (
        UniverseCheck().evaluate(make_intent(qty=D("1000")), ctx, CFG).decision
        == "allow"
    )
    res = UniverseCheck().evaluate(make_intent(qty=D("1001")), ctx, CFG)
    assert (res.decision, res.reason) == ("clamp", ReasonCode.ADV_EXCEEDED)
    assert res.clamped_qty == D("1000")


def test_universe_adv_floor_zero_rejects():
    ctx = make_ctx(adv={"SPY": D("50")})  # 0.01 * 50 = 0.5 -> floor 0
    res = UniverseCheck().evaluate(make_intent(qty=D("1")), ctx, CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.ADV_EXCEEDED)


def test_universe_adv_skips_notional_intents():
    assert UniverseCheck().evaluate(crypto_intent(), make_ctx(), CFG).decision == "allow"


# ------------------------------------------------------------------- long only


def test_long_only_buy_ignored():
    assert LongOnlyCheck().evaluate(make_intent(), make_ctx(), CFG).decision == "allow"


def test_long_only_missing_sleeve_view():
    res = LongOnlyCheck().evaluate(
        make_intent(side=Side.SELL, sleeve_id="ghost"), make_ctx(), CFG
    )
    assert (res.decision, res.reason) == ("reject", ReasonCode.SHORT_FORBIDDEN)


@pytest.mark.parametrize(
    ("qty", "protective", "decision", "clamped"),
    [
        ("5", False, "allow", None),  # exact holding sells clean
        ("6", False, "reject", None),
        ("6", True, "clamp", "5"),  # protective over-sell clamps to held
    ],
)
def test_long_only_sell_boundaries(qty, protective, decision, clamped):
    ctx = make_ctx(sleeve_positions=(pos("SPY", "5"),))
    res = LongOnlyCheck().evaluate(
        make_intent(side=Side.SELL, qty=D(qty), is_protective=protective), ctx, CFG
    )
    assert res.decision == decision
    if decision != "allow":
        assert res.reason is ReasonCode.PHANTOM_POSITION
    if clamped is not None:
        assert res.clamped_qty == D(clamped)


def test_long_only_nothing_held_rejects_even_protective():
    res = LongOnlyCheck().evaluate(
        make_intent(side=Side.SELL, qty=D("1"), is_protective=True), make_ctx(), CFG
    )
    assert (res.decision, res.reason) == ("reject", ReasonCode.PHANTOM_POSITION)


def test_long_only_notional_sell():
    held = make_ctx(sleeve_positions=(pos("BTC/USD", "1"),))
    assert (
        LongOnlyCheck().evaluate(crypto_intent(side=Side.SELL), held, CFG).decision
        == "allow"
    )
    res = LongOnlyCheck().evaluate(crypto_intent(side=Side.SELL), make_ctx(), CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.PHANTOM_POSITION)


# ------------------------------------------------------------------ order size


def test_market_order_forbidden_belt():
    # The contracts validator makes this unrepresentable; bypass it deliberately.
    bad = OrderIntent.model_construct(**{**make_intent().model_dump(), "limit_price": None})
    res = OrderSizeCheck().evaluate(bad, make_ctx(), CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.MARKET_ORDER_FORBIDDEN)


@pytest.mark.parametrize(
    ("qty", "limit", "decision", "reason", "clamped"),
    [
        ("5", "5.00", "allow", None, None),  # exactly min notional 25
        ("4", "6.00", "reject", ReasonCode.MIN_NOTIONAL, None),  # 24 < 25
        ("200", "2", "allow", None, None),  # exactly max shares
        ("201", "2", "clamp", ReasonCode.ORDER_SHARES_CAP, "200"),
        ("5", "100", "allow", None, None),  # exactly max notional 500
        ("6", "100", "clamp", ReasonCode.ORDER_NOTIONAL_CAP, "5"),
        ("1", "600", "reject", ReasonCode.ORDER_NOTIONAL_CAP, None),  # floor -> 0
        ("20", "33.33", "clamp", ReasonCode.ORDER_NOTIONAL_CAP, "15"),  # 666.60 -> floor(500/33.33)
    ],
)
def test_order_size_qty_boundaries(qty, limit, decision, reason, clamped):
    res = OrderSizeCheck().evaluate(
        make_intent(qty=D(qty), limit_price=D(limit)), make_ctx(), CFG
    )
    assert (res.decision, res.reason) == (decision, reason)
    if clamped is not None:
        assert res.clamped_qty == D(clamped)


def test_order_size_sleeve_cap_qty():
    res = OrderSizeCheck().evaluate(
        make_intent(sleeve_id="tight", qty=D("2"), limit_price=D("60")), make_ctx(), CFG
    )
    assert (res.decision, res.reason) == ("clamp", ReasonCode.SLEEVE_ORDER_CAP)
    assert res.clamped_qty == D("1")  # floor(100 / 60)


def test_order_size_most_binding_cap_wins():
    # shares cap -> 200, notional cap -> 166, sleeve cap -> 33: min wins
    res = OrderSizeCheck().evaluate(
        make_intent(sleeve_id="tight", qty=D("300"), limit_price=D("3")), make_ctx(), CFG
    )
    assert (res.decision, res.reason) == ("clamp", ReasonCode.SLEEVE_ORDER_CAP)
    assert res.clamped_qty == D("33")


@pytest.mark.parametrize(
    ("sleeve", "notional", "decision", "reason", "clamped"),
    [
        ("big", "500", "allow", None, None),  # exactly max notional
        ("big", "501", "clamp", ReasonCode.ORDER_NOTIONAL_CAP, "500"),
        ("tight", "100", "allow", None, None),  # exactly sleeve max order
        ("tight", "150", "clamp", ReasonCode.SLEEVE_ORDER_CAP, "100"),
        ("tight", "24.99", "reject", ReasonCode.MIN_NOTIONAL, None),
    ],
)
def test_order_size_notional_boundaries(sleeve, notional, decision, reason, clamped):
    res = OrderSizeCheck().evaluate(
        crypto_intent(sleeve_id=sleeve, notional=D(notional)), make_ctx(), CFG
    )
    assert (res.decision, res.reason) == (decision, reason)
    if clamped is not None:
        assert res.clamped_notional == D(clamped)


def test_order_size_unknown_sleeve_rejects():
    res = OrderSizeCheck().evaluate(make_intent(sleeve_id="ghost"), make_ctx(), CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.SLEEVE_BUDGET_EXCEEDED)
    assert "no budget configured" in res.detail


# ---------------------------------------------------------------- price collar


@pytest.mark.parametrize(
    ("limit", "decision"),
    [
        ("102", "allow"),  # exactly 2% from reference 100
        ("102.01", "reject"),
        ("98", "allow"),
        ("97.99", "reject"),
    ],
)
def test_collar_equity_boundary(limit, decision):
    res = PriceCollarCheck().evaluate(make_intent(limit_price=D(limit)), make_ctx(), CFG)
    assert res.decision == decision
    if decision == "reject":
        assert res.reason is ReasonCode.PRICE_COLLAR_BREACH


@pytest.mark.parametrize(("limit", "decision"), [("105", "allow"), ("105.01", "reject")])
def test_collar_crypto_boundary(limit, decision):
    intent = make_intent(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        qty=D("1"),
        limit_price=D(limit),
    )
    res = PriceCollarCheck().evaluate(intent, make_ctx(), CFG)
    assert res.decision == decision


def test_suspect_quote_boundary():
    # mid 101 vs last 100 = exactly 1.0% divergence: not suspect, collar vs mid
    edge = make_ctx(quotes={"SPY": quote(bid="100", ask="102", last="100")})
    res = PriceCollarCheck().evaluate(make_intent(limit_price=D("101")), edge, CFG)
    assert res.decision == "allow"
    over = make_ctx(quotes={"SPY": quote(bid="100", ask="102.2", last="100")})
    res = PriceCollarCheck().evaluate(make_intent(limit_price=D("101")), over, CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.SUSPECT_QUOTE)


def test_collar_skips_notional_intents():
    assert (
        PriceCollarCheck().evaluate(crypto_intent(), make_ctx(), CFG).decision == "allow"
    )


# -------------------------------------------------------------------- exposure


def test_exposure_sells_and_reductions_exempt():
    ctx = make_ctx(account_positions=(pos("SPY", "10"),))  # symbol already at cap
    reduce = make_intent(qty=D("1"), reduces_position=True)
    assert ExposureCheck().evaluate(reduce, ctx, CFG).decision == "allow"
    sell = make_intent(side=Side.SELL, qty=D("1"))
    assert ExposureCheck().evaluate(sell, ctx, CFG).decision == "allow"


def test_exposure_symbol_cap_boundary():
    ctx = make_ctx()
    assert ExposureCheck().evaluate(make_intent(qty=D("10")), ctx, CFG).decision == "allow"
    res = ExposureCheck().evaluate(make_intent(qty=D("11")), ctx, CFG)
    assert (res.decision, res.reason) == ("clamp", ReasonCode.SYMBOL_EXPOSURE_CAP)
    assert res.clamped_qty == D("10")


def test_exposure_symbol_cap_counts_position_and_open_buys():
    ctx = make_ctx(account_positions=(pos("SPY", "5"),), open_orders=(oref(qty="3"),))
    # 500 held + 300 open + 300 added = 1100 > 1000; headroom 200 -> 2 shares
    res = ExposureCheck().evaluate(make_intent(qty=D("3")), ctx, CFG)
    assert (res.decision, res.reason) == ("clamp", ReasonCode.SYMBOL_EXPOSURE_CAP)
    assert res.clamped_qty == D("2")


def test_exposure_symbol_cap_no_headroom_rejects():
    ctx = make_ctx(account_positions=(pos("SPY", "10"),))
    res = ExposureCheck().evaluate(make_intent(qty=D("1")), ctx, CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.SYMBOL_EXPOSURE_CAP)


def test_exposure_symbol_cap_decimal_boundary():
    ctx = make_ctx(
        quotes={"SPY": quote(last="100.10", bid="100.05", ask="100.15")},
        account_positions=(pos("SPY", "3"),),
    )
    # 3 * 100.10 + 7 * 100.10 = 1001.00; headroom 699.70 -> floor 6 shares
    res = ExposureCheck().evaluate(
        make_intent(qty=D("7"), limit_price=D("100.10")), ctx, CFG
    )
    assert (res.decision, res.reason) == ("clamp", ReasonCode.SYMBOL_EXPOSURE_CAP)
    assert res.clamped_qty == D("6")


def _gross_quotes():
    return {"SPY": quote(), "AAA": quote(symbol="AAA", bid=None, ask=None)}


def test_exposure_gross_cap_boundary():
    exact = make_ctx(quotes=_gross_quotes(), account_positions=(pos("AAA", "84"),))
    assert ExposureCheck().evaluate(make_intent(qty=D("6")), exact, CFG).decision == "allow"
    over = make_ctx(
        quotes=_gross_quotes(),
        account_positions=(pos("AAA", "80"),),
        open_orders=(oref(symbol="BBB", qty="5"),),
    )
    # 8000 + 500 open + 600 added = 9100; headroom 500 -> 5 shares
    res = ExposureCheck().evaluate(make_intent(qty=D("6")), over, CFG)
    assert (res.decision, res.reason) == ("clamp", ReasonCode.GROSS_EXPOSURE_CAP)
    assert res.clamped_qty == D("5")


def test_exposure_gross_cap_rejects_when_no_share_fits():
    ctx = make_ctx(quotes=_gross_quotes(), account_positions=(pos("AAA", "89"),))
    res = ExposureCheck().evaluate(make_intent(qty=D("2"), limit_price=D("150")), ctx, CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.GROSS_EXPOSURE_CAP)


def test_exposure_position_count_cap():
    others = tuple(pos(f"S{i}", "1") for i in range(12))  # marked at avg_cost
    res = ExposureCheck().evaluate(make_intent(), make_ctx(account_positions=others), CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.POSITION_COUNT_CAP)
    held = others[:11] + (pos("SPY", "1"),)  # adding to a held symbol is fine
    assert (
        ExposureCheck().evaluate(make_intent(), make_ctx(account_positions=held), CFG).decision
        == "allow"
    )


def test_exposure_crypto_sleeve_cap_boundary():
    ctx = make_ctx(sleeve_positions=(pos("BTC/USD", "5"),))  # 5 * ref 100 = 500
    assert (
        ExposureCheck().evaluate(crypto_intent(notional=D("250")), ctx, CFG).decision
        == "allow"
    )
    res = ExposureCheck().evaluate(crypto_intent(notional=D("251")), ctx, CFG)
    assert (res.decision, res.reason) == ("clamp", ReasonCode.CRYPTO_SLEEVE_CAP)
    assert res.clamped_notional == D("250")
    full = make_ctx(sleeve_positions=(pos("BTC/USD", "7.5"),))
    res = ExposureCheck().evaluate(crypto_intent(notional=D("10")), full, CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.CRYPTO_SLEEVE_CAP)


def test_exposure_sleeve_budget_boundary():
    exact = make_ctx(sleeve_positions=(pos("SLV", "24"),))  # marked at avg_cost 100
    assert ExposureCheck().evaluate(make_intent(qty=D("1")), exact, CFG).decision == "allow"
    res = ExposureCheck().evaluate(make_intent(qty=D("2")), exact, CFG)
    assert (res.decision, res.reason) == ("clamp", ReasonCode.SLEEVE_BUDGET_EXCEEDED)
    assert res.clamped_qty == D("1")


def test_exposure_sleeve_budget_charges_open_buys():
    ctx = make_ctx(
        sleeve_positions=(pos("SLV", "20"),), open_orders=(oref(symbol="XYZ", qty="4"),)
    )
    # 2000 + 400 open + 200 added = 2600 > 2500; headroom 100 -> 1 share
    res = ExposureCheck().evaluate(make_intent(qty=D("2")), ctx, CFG)
    assert (res.decision, res.reason) == ("clamp", ReasonCode.SLEEVE_BUDGET_EXCEEDED)
    assert res.clamped_qty == D("1")


def test_exposure_unknown_sleeve_rejects():
    res = ExposureCheck().evaluate(make_intent(sleeve_id="ghost"), make_ctx(), CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.SLEEVE_BUDGET_EXCEEDED)
    assert "no budget configured" in res.detail


def test_exposure_most_binding_clamp_wins():
    ctx = make_ctx(
        account_positions=(pos("SPY", "5"),), sleeve_positions=(pos("SLV", "22"),)
    )
    # symbol headroom 500 -> 5 shares; sleeve budget headroom 300 -> 3 shares
    res = ExposureCheck().evaluate(make_intent(qty=D("10")), ctx, CFG)
    assert (res.decision, res.reason) == ("clamp", ReasonCode.SLEEVE_BUDGET_EXCEEDED)
    assert res.clamped_qty == D("3")


# ------------------------------------------------------------------ duplicates


@pytest.mark.parametrize(
    ("age", "symbol", "side", "sleeve", "decision"),
    [
        (89, "SPY", Side.BUY, "s1", "reject"),
        (90, "SPY", Side.BUY, "s1", "allow"),  # exactly the window is out of it
        (10, "SPY", Side.SELL, "s1", "allow"),
        (10, "AAA", Side.BUY, "s1", "allow"),
        (10, "SPY", Side.BUY, "tight", "allow"),
    ],
)
def test_duplicate_window(age, symbol, side, sleeve, decision):
    ctx = make_ctx(recent_orders=(recent(symbol=symbol, side=side, sleeve=sleeve, age_s=age),))
    res = DuplicateCheck().evaluate(make_intent(), ctx, CFG)
    assert res.decision == decision
    if decision == "reject":
        assert res.reason is ReasonCode.DUPLICATE_WINDOW


def test_open_entry_exists():
    ctx = make_ctx(open_orders=(oref(),))
    res = DuplicateCheck().evaluate(make_intent(), ctx, CFG)
    assert (res.decision, res.reason) == ("reject", ReasonCode.OPEN_ENTRY_EXISTS)
    # enforced across sleeves: OrderRef has no sleeve, and sleeves net anyway
    res = DuplicateCheck().evaluate(make_intent(sleeve_id="tight"), ctx, CFG)
    assert res.reason is ReasonCode.OPEN_ENTRY_EXISTS
    assert DuplicateCheck().evaluate(make_intent(reduces_position=True), ctx, CFG).decision == "allow"
    sells = make_ctx(open_orders=(oref(side=Side.SELL),))
    assert DuplicateCheck().evaluate(make_intent(), sells, CFG).decision == "allow"
    off = CFG.model_copy(
        update={"duplicates": DuplicateRules(one_open_entry_per_symbol_per_sleeve=False)}
    )
    assert DuplicateCheck().evaluate(make_intent(), ctx, off).decision == "allow"


# ------------------------------------------------------------------ rate limit


def _spread(n, age_s):
    return tuple(recent(symbol=f"G{i}", age_s=age_s) for i in range(n))


@pytest.mark.parametrize(
    ("recents", "protective", "decision", "reason"),
    [
        (_spread(4, 10), False, "allow", None),
        (_spread(5, 10), False, "reject", ReasonCode.RATE_GLOBAL_MIN),
        (_spread(5, 60), False, "allow", None),  # exactly 60s old leaves the window
        ((recent(age_s=10),), False, "allow", None),
        ((recent(age_s=10), recent(age_s=20)), False, "reject", ReasonCode.RATE_SYMBOL_MIN),
        ((recent(age_s=10), recent(age_s=20)), True, "reject", ReasonCode.RATE_SYMBOL_MIN),
        (_spread(40, 120), False, "reject", ReasonCode.RATE_GLOBAL_DAY),
        (_spread(40, 120), True, "allow", None),  # protective bypasses day caps
        (_spread(40, 86400), False, "allow", None),  # exactly 24h old leaves the window
        (tuple(recent(age_s=120 + i) for i in range(6)), False, "reject", ReasonCode.RATE_SYMBOL_DAY),
        (tuple(recent(age_s=120 + i) for i in range(5)), False, "allow", None),
        (tuple(recent(age_s=120 + i) for i in range(6)), True, "allow", None),
    ],
)
def test_rate_limits(recents, protective, decision, reason):
    ctx = make_ctx(recent_orders=recents)
    res = RateLimitCheck().evaluate(make_intent(is_protective=protective), ctx, CFG)
    assert (res.decision, res.reason) == (decision, reason)


# -------------------------------------------------------------------- cooldown


@pytest.mark.parametrize(
    ("until_offset_s", "symbol", "reduces", "decision"),
    [
        (1, "SPY", False, "reject"),
        (0, "SPY", False, "allow"),  # until == now has expired
        (1, "SPY", True, "allow"),
        (1, "AAA", False, "allow"),
    ],
)
def test_cooldown(until_offset_s, symbol, reduces, decision):
    ctx = make_ctx(
        cooldowns=(
            SymbolCooldown(symbol=symbol, until=NOW + timedelta(seconds=until_offset_s)),
        )
    )
    res = CooldownCheck().evaluate(make_intent(reduces_position=reduces), ctx, CFG)
    assert res.decision == decision
    if decision == "reject":
        assert res.reason is ReasonCode.SYMBOL_COOLDOWN


# -------------------------------------------------------------- default_checks


def test_default_checks_catalog_order():
    names = [c.name for c in default_checks()]
    assert names[0] == "state_gate"
    assert names[-1] == "exposure"
    assert len(names) == 11
    assert len(set(names)) == 11
