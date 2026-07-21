from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from nwt_contracts import (
    AssetClass,
    OrderIntent,
    PortfolioView,
    RiskContext,
    SessionInfo,
    Side,
    TradingState,
)
from nwt_risk import GovernorContext, QuoteView, ReasonCode, RiskGovernor
from nwt_risk.checks import default_checks
from nwt_risk.checks.base import allow, clamp, reject
from nwt_risk.config import RiskConfig, SleeveBudget

NOW = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)  # Tue 10:30 ET, XNYS open

CFG = RiskConfig(
    sleeves={"s1": SleeveBudget(budget_usd=D("2500"), max_order_usd=D("500"))},
    config_hash="cfg-test",
)


class Stub:
    """PreTradeCheck double returning a fixed result and logging invocation."""

    def __init__(self, name, result=None, log=None):
        self.name = name
        self._result = result
        self._log = log

    def evaluate(self, intent, ctx, cfg):
        if self._log is not None:
            self._log.append(self.name)
        return self._result if self._result is not None else allow(self.name)


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


def make_ctx(state=TradingState.ACTIVE) -> GovernorContext:
    base = RiskContext(
        ts=NOW,
        mode="paper",
        trading_state=state,
        account=PortfolioView(
            scope="account", ts=NOW, cash=D("10000"), equity=D("10000")
        ),
        sleeves=(
            PortfolioView(scope="s1", ts=NOW, cash=D("1000"), equity=D("2500")),
        ),
        sessions=(SessionInfo(calendar="XNYS", is_open=True),),
        last_reconcile_age_s=10.0,
    )
    return GovernorContext(
        base=base,
        quotes={
            "SPY": QuoteView(
                symbol="SPY", ts=NOW, last=D("100"), bid=D("99.95"), ask=D("100.05")
            )
        },
        adv_by_symbol={"SPY": D("100000")},
    )


def make_governor(checks, state=TradingState.ACTIVE, audit=None) -> RiskGovernor:
    return RiskGovernor(checks, CFG, state_fn=lambda: state, audit=audit)


def test_all_checks_run_even_after_reject():
    log = []
    checks = [
        Stub("a", reject("a", ReasonCode.STALE_QUOTE, "x"), log),
        Stub("b", clamp("b", ReasonCode.ORDER_SHARES_CAP, "x", qty=D("1")), log),
        Stub("c", None, log),
    ]
    out = make_governor(checks).review([make_intent()], make_ctx())
    verdict = out.verdicts[0]
    assert log == ["a", "b", "c"]  # no short-circuit after the reject
    assert len(verdict.results) == 3
    assert verdict.decision == "reject"  # reject dominates the clamp
    assert verdict.reject_reasons == [ReasonCode.STALE_QUOTE]
    assert out.approved == ()


def test_multiple_qty_clamps_min_wins():
    checks = [
        Stub("a", clamp("a", ReasonCode.ORDER_SHARES_CAP, "x", qty=D("5"))),
        Stub("b", clamp("b", ReasonCode.SYMBOL_EXPOSURE_CAP, "x", qty=D("3"))),
    ]
    out = make_governor(checks).review([make_intent(qty=D("10"))], make_ctx())
    assert out.verdicts[0].decision == "clamp"
    assert out.approved[0].approved_qty == D("3")


def test_multiple_notional_clamps_min_wins():
    checks = [
        Stub("a", clamp("a", ReasonCode.ORDER_NOTIONAL_CAP, "x", notional=D("450"))),
        Stub("b", clamp("b", ReasonCode.CRYPTO_SLEEVE_CAP, "x", notional=D("400"))),
    ]
    intent = make_intent(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        qty=None,
        notional=D("500"),
        limit_price=None,
    )
    out = make_governor(checks).review([intent], make_ctx())
    assert out.approved[0].approved_notional == D("400")


def test_clamp_to_zero_drops_order_with_reasons_logged():
    checks = [Stub("a", clamp("a", ReasonCode.ADV_EXCEEDED, "x", qty=D("0")))]
    out = make_governor(checks).review([make_intent()], make_ctx())
    assert out.approved == ()
    verdict = out.verdicts[0]
    assert verdict.decision == "clamp"  # audit shows the clamp, not a fake reject
    assert verdict.results[0].reason is ReasonCode.ADV_EXCEEDED


def test_halted_structurally_rejects_even_reducers():
    out = make_governor([Stub("a")], state=TradingState.HALTED).review(
        [make_intent(side=Side.SELL, reduces_position=True)],
        make_ctx(state=TradingState.HALTED),
    )
    verdict = out.verdicts[0]
    assert verdict.decision == "reject"
    assert out.approved == ()
    assert len(verdict.results) == 2  # the stub plus the structural gate
    structural = verdict.results[-1]
    assert structural.check == "structural_state_gate"
    assert structural.reason is ReasonCode.STATE_NOT_ACTIVE


def test_reducing_passes_only_reducers():
    intents = [
        make_intent(intent_id="entry"),
        make_intent(intent_id="exit", side=Side.SELL, reduces_position=True),
    ]
    out = make_governor([Stub("a")], state=TradingState.REDUCING).review(
        intents, make_ctx(state=TradingState.REDUCING)
    )
    assert [a.intent.intent_id for a in out.approved] == ["exit"]
    by_id = {v.intent_id: v for v in out.verdicts}
    assert by_id["entry"].decision == "reject"
    assert by_id["entry"].reject_reasons == [ReasonCode.STATE_NOT_ACTIVE]
    assert by_id["exit"].decision == "allow"


def test_approvals_carry_config_hash_and_increasing_ids():
    gov = make_governor([Stub("a")])
    ctx = make_ctx()
    out = gov.review([make_intent(intent_id="i1"), make_intent(intent_id="i2")], ctx)
    assert [a.approval_id for a in out.approved] == ["gov-1", "gov-2"]
    assert all(v.config_hash == "cfg-test" for v in out.verdicts)
    assert all(a.approved_at == ctx.now for a in out.approved)
    again = gov.review([make_intent(intent_id="i3")], ctx)
    assert again.approved[0].approval_id == "gov-3"  # sequence survives across calls


def test_approved_order_never_exceeds_intent_size():
    checks = [Stub("a", clamp("a", ReasonCode.ORDER_SHARES_CAP, "x", qty=D("3")))]
    out = make_governor(checks).review([make_intent(qty=D("5"))], make_ctx())
    assert out.approved[0].approved_qty == D("3") <= D("5")
    # A clamp that tries to size UP trips the ApprovedOrder belt, loudly.
    up = [Stub("a", clamp("a", ReasonCode.ORDER_SHARES_CAP, "x", qty=D("20")))]
    with pytest.raises(ValidationError, match="never up"):
        make_governor(up).review([make_intent(qty=D("5"))], make_ctx())


def test_audit_called_per_intent():
    events = []
    gov = make_governor(
        [Stub("a", reject("a", ReasonCode.STALE_QUOTE, "x"))],
        audit=lambda kind, payload: events.append((kind, payload)),
    )
    gov.review([make_intent(intent_id="i1"), make_intent(intent_id="i2")], make_ctx())
    assert [k for k, _ in events] == ["verdict", "verdict"]
    assert all(p["decision"] == "reject" for _, p in events)
    assert all(p["config_hash"] == "cfg-test" for _, p in events)


def test_default_checks_end_to_end_allow():
    gov = RiskGovernor(default_checks(), CFG, state_fn=lambda: TradingState.ACTIVE)
    out = gov.review([make_intent()], make_ctx())
    verdict = out.verdicts[0]
    assert verdict.decision == "allow"
    assert len(verdict.results) == 11
    assert verdict.results[0].check == "state_gate"
    assert verdict.results[-1].check == "exposure"
    assert out.approved[0].approved_qty == D("4")


def test_default_checks_end_to_end_stale_quote_rejects():
    gov = RiskGovernor(default_checks(), CFG, state_fn=lambda: TradingState.ACTIVE)
    ctx = make_ctx()
    stale = ctx.model_copy(
        update={
            "quotes": {
                "SPY": QuoteView(
                    symbol="SPY", ts=NOW - timedelta(seconds=31), last=D("100")
                )
            }
        }
    )
    out = gov.review([make_intent()], stale)
    assert out.verdicts[0].decision == "reject"
    assert ReasonCode.STALE_QUOTE in out.verdicts[0].reject_reasons
    assert len(out.verdicts[0].results) == 11  # every check still ran
