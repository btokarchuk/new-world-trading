"""Conservation property tests for the SleeveAllocator.

These are the invariants the whole sleeve model rests on: netting and
allocation may never create or destroy shares, cash, or fees.
"""

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from nwt_contracts import ApprovedOrder, AssetClass, OrderIntent, Side
from nwt_engine.sleeves.allocator import (
    allocate_fill,
    build_net_plans,
    cross_price,
)

_TS = datetime(2024, 1, 2, 21, tzinfo=UTC)


def _approved(sleeve: str, side: Side, qty: int, limit: str, seq: int) -> ApprovedOrder:
    intent = OrderIntent(
        intent_id=f"i-{sleeve}-{seq}",
        sleeve_id=sleeve,
        strategy="test",
        symbol="TEST",
        asset_class=AssetClass.ETF,
        side=side,
        qty=Decimal(qty),
        limit_price=Decimal(limit),
        as_of=_TS,
        created_at=_TS,
        reduces_position=side is Side.SELL,
    )
    return ApprovedOrder(
        intent=intent,
        approved_qty=intent.qty,
        approved_notional=None,
        approval_id=f"a-{sleeve}-{seq}",
        approved_at=_TS,
    )


legs_strategy = st.lists(
    st.tuples(
        st.sampled_from(["s1", "s2", "s3", "s4"]),
        st.sampled_from([Side.BUY, Side.SELL]),
        st.integers(min_value=1, max_value=500),
    ),
    min_size=1,
    max_size=8,
)


@given(legs_strategy)
@settings(max_examples=300, deadline=None)
def test_netting_conserves_shares_and_sides(raw_legs):
    approved = [
        _approved(sleeve, side, qty, "100.00" if side is Side.BUY else "99.00", i)
        for i, (sleeve, side, qty) in enumerate(raw_legs)
    ]
    plans = build_net_plans(approved)
    assert len(plans) == 1
    plan = plans[0]

    buy_total = sum(
        (a.approved_qty for a in approved if a.intent.side is Side.BUY), Decimal("0")
    )
    sell_total = sum(
        (a.approved_qty for a in approved if a.intent.side is Side.SELL), Decimal("0")
    )

    # With compatible limits (buy 100 > sell 99) netting always happens.
    assert not plan.unnetted_legs

    cross_buys = sum((c.qty for c in plan.crosses if c.side is Side.BUY), Decimal("0"))
    cross_sells = sum((c.qty for c in plan.crosses if c.side is Side.SELL), Decimal("0"))
    # Invariant: crosses are balanced and equal the overlap exactly.
    assert cross_buys == cross_sells == min(buy_total, sell_total)
    # Invariant: every cross quantity is a whole share (ETF).
    assert all(c.qty == c.qty.to_integral_value() for c in plan.crosses)

    # Invariant: residual + crossed == original, per side.
    residual_total = sum((leg.qty for leg in plan.residual_legs), Decimal("0"))
    assert residual_total == plan.net_qty == abs(buy_total - sell_total)
    if plan.net_qty > 0:
        expected_side = Side.BUY if buy_total > sell_total else Side.SELL
        assert plan.net_side is expected_side
        assert all(leg.side is expected_side for leg in plan.residual_legs)
    else:
        assert plan.net_side is None


@given(
    legs_strategy,
    st.integers(min_value=0, max_value=2000),
    st.integers(min_value=0, max_value=500),  # fees in cents
)
@settings(max_examples=300, deadline=None)
def test_allocation_conserves_qty_and_fees(raw_legs, filled_raw, fees_cents):
    approved = [
        _approved(sleeve, side, qty, "100.00" if side is Side.BUY else "99.00", i)
        for i, (sleeve, side, qty) in enumerate(raw_legs)
    ]
    plan = build_net_plans(approved)[0]
    if plan.net_qty == 0:
        return
    filled = min(Decimal(filled_raw), plan.net_qty)
    fees = Decimal(fees_cents) / 100
    allocations = allocate_fill(plan, filled, fees)

    # Invariant: allocations conserve the filled quantity exactly.
    assert sum((a.qty for a in allocations), Decimal("0")) == filled
    # Invariant: fees conserved to the cent.
    if filled > 0:
        assert sum((a.fees for a in allocations), Decimal("0")) == fees
    # Invariant: whole shares, and no leg over-allocated.
    residual_by_sleeve: dict[str, Decimal] = {}
    for leg in plan.residual_legs:
        residual_by_sleeve[leg.sleeve_id] = (
            residual_by_sleeve.get(leg.sleeve_id, Decimal("0")) + leg.qty
        )
    got: dict[str, Decimal] = {}
    for alloc in allocations:
        assert alloc.qty == alloc.qty.to_integral_value()
        got[alloc.sleeve_id] = got.get(alloc.sleeve_id, Decimal("0")) + alloc.qty
    for sleeve_id, qty in got.items():
        assert qty <= residual_by_sleeve[sleeve_id]


def test_incompatible_limits_skip_netting():
    approved = [
        _approved("s1", Side.BUY, 100, "98.00", 0),   # buyer caps at 98
        _approved("s2", Side.SELL, 60, "99.00", 1),   # seller demands 99
    ]
    plan = build_net_plans(approved)[0]
    assert len(plan.unnetted_legs) == 2
    assert not plan.crosses
    assert plan.net_qty == 0


def test_cross_price_clamped():
    approved = [
        _approved("s1", Side.BUY, 100, "100.00", 0),
        _approved("s2", Side.SELL, 60, "99.00", 1),
    ]
    plan = build_net_plans(approved)[0]
    assert cross_price(plan, Decimal("99.50")) == Decimal("99.50")   # inside interval
    assert cross_price(plan, Decimal("101.00")) == Decimal("100.00")  # capped at buy limit
    assert cross_price(plan, Decimal("98.00")) == Decimal("99.00")    # floored at sell limit


def test_notional_orders_pass_through_unnetted():
    intent = OrderIntent(
        intent_id="i-crypto",
        sleeve_id="crypto",
        strategy="test",
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        side=Side.BUY,
        notional=Decimal("100"),
        as_of=_TS,
        created_at=_TS,
    )
    approved = ApprovedOrder(
        intent=intent,
        approved_qty=None,
        approved_notional=intent.notional,
        approval_id="a-crypto",
        approved_at=_TS,
    )
    assert build_net_plans([approved]) == []
