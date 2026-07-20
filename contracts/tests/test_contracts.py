from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from nwt_contracts import ApprovedOrder, AssetClass, OrderIntent, Side, TradingState

TS = datetime(2026, 1, 5, tzinfo=UTC)


def _intent(**overrides) -> OrderIntent:
    base = dict(
        intent_id="i1",
        sleeve_id="s1",
        strategy="test",
        symbol="SPY",
        asset_class=AssetClass.ETF,
        side=Side.BUY,
        qty=Decimal("10"),
        limit_price=Decimal("500"),
        as_of=TS,
        created_at=TS,
    )
    base.update(overrides)
    return OrderIntent(**base)


def test_equity_requires_limit_price():
    with pytest.raises(ValidationError, match="limit price"):
        _intent(limit_price=None)


def test_equity_requires_whole_shares():
    with pytest.raises(ValidationError, match="whole shares"):
        _intent(qty=Decimal("1.5"))


def test_qty_xor_notional():
    with pytest.raises(ValidationError):
        _intent(notional=Decimal("100"))  # both set
    with pytest.raises(ValidationError):
        _intent(qty=None)  # neither set


def test_crypto_notional_allowed_without_limit():
    intent = _intent(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        qty=None,
        notional=Decimal("100"),
        limit_price=None,
    )
    assert intent.notional == 100


def test_governor_can_never_size_up():
    intent = _intent()
    with pytest.raises(ValidationError, match="never up"):
        ApprovedOrder(
            intent=intent,
            approved_qty=Decimal("11"),
            approval_id="a1",
            approved_at=TS,
        )
    ok = ApprovedOrder(
        intent=intent, approved_qty=Decimal("5"), approval_id="a1", approved_at=TS
    )
    assert ok.approved_qty == 5


def test_trading_state_gates():
    assert TradingState.ACTIVE.allows(reduces_position=False)
    assert not TradingState.REDUCING.allows(reduces_position=False)
    assert TradingState.REDUCING.allows(reduces_position=True)
    assert not TradingState.HALTED.allows(reduces_position=True)
