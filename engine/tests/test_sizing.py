

def test_crypto_sizes_fractionally_instead_of_flooring_to_zero():
    """Regression: whole-unit flooring pinned a $750 BTC sleeve at zero forever,
    so the crypto sleeve never deployed. Alpaca's crypto increment is 0.0001."""
    from decimal import Decimal

    from nwt_engine.execution.sizing import _floor_to_increment

    # $750 of BTC at $100k is 0.0075 — zero under whole-unit flooring.
    qty = Decimal("750") / Decimal("100000")
    assert _floor_to_increment(qty, Decimal("1")) == Decimal("0")
    assert _floor_to_increment(qty, Decimal("0.0001")) == Decimal("0.0075")

    # Never rounds UP past the target weight.
    assert _floor_to_increment(Decimal("0.00019"), Decimal("0.0001")) == Decimal("0.0001")
    # Equities keep whole-share behaviour exactly.
    assert _floor_to_increment(Decimal("9.99"), Decimal("1")) == Decimal("9")
