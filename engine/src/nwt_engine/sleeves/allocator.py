"""SleeveAllocator: cross-sleeve netting and fill allocation.

Rules (see plan):
- All approved orders in a cycle are grouped by symbol. Only the net residual
  is submitted to the broker — never opposing same-symbol orders in one cycle.
- The overlapped quantity becomes internal crosses: fee-free synthetic fills,
  one per participating leg side, executed at the NEXT bar's open (same
  reference price both sides), clamped into [max sell limit, min buy limit].
  If that interval is empty the symbol is not netted this cycle (no fair cross
  price exists) and all legs go to the broker individually.
- External fills on the residual are allocated pro-rata by residual quantity
  across net-side legs, largest-remainder on whole shares for equities so
  share integrality is preserved exactly. Fees are allocated in proportion to
  allocated quantity, with the rounding remainder assigned to the largest leg
  so fee conservation is exact.
- Conservation invariants (property-tested): summed allocations equal the
  filled quantity exactly; each leg never receives more than its residual;
  cross buy and sell quantities are equal; fees are conserved to the cent.

Notional (crypto) intents are not netted in v1 — they pass through unchanged.
"""

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

from pydantic import BaseModel

from nwt_contracts import ApprovedOrder, AssetClass, Side


class Leg(BaseModel, frozen=True):
    sleeve_id: str
    strategy: str
    side: Side
    qty: Decimal
    limit_price: Decimal | None
    intent_id: str


class CrossFill(BaseModel, frozen=True):
    sleeve_id: str
    side: Side
    qty: Decimal


class NetPlan(BaseModel, frozen=True):
    symbol: str
    asset_class: AssetClass
    #: legs on the net (residual) side with their residual quantities
    residual_legs: tuple[Leg, ...]
    net_side: Side | None            # None => pure cross, no broker order
    net_qty: Decimal
    net_limit: Decimal | None        # most conservative limit among residual legs
    #: internal crosses to execute at next bar open (both sides, equal totals)
    crosses: tuple[CrossFill, ...]
    #: fair-cross clamp: [cross_floor, cross_cap] = [max sell limit, min buy limit]
    cross_floor: Decimal | None = None
    cross_cap: Decimal | None = None
    #: legs submitted individually because no fair cross price existed
    unnetted_legs: tuple[Leg, ...] = ()


class Allocation(BaseModel, frozen=True):
    sleeve_id: str
    qty: Decimal
    fees: Decimal


def _leg(approved: ApprovedOrder) -> Leg:
    intent = approved.intent
    assert approved.approved_qty is not None
    return Leg(
        sleeve_id=intent.sleeve_id,
        strategy=intent.strategy,
        side=intent.side,
        qty=approved.approved_qty,
        limit_price=intent.limit_price,
        intent_id=intent.intent_id,
    )


def _split_prorata(
    total: Decimal, weights: list[Decimal], whole_shares: bool
) -> list[Decimal]:
    """Split `total` across weights, conserving the sum exactly.

    Whole-share mode uses largest-remainder on integral shares; fractional mode
    quantizes to 1e-6 and assigns the dust to the largest weight.
    """
    weight_sum = sum(weights)
    if weight_sum == 0:
        return [Decimal("0")] * len(weights)
    quantum = Decimal("1") if whole_shares else Decimal("0.000001")
    raw = [total * w / weight_sum for w in weights]
    floored = [r.quantize(quantum, rounding=ROUND_DOWN) for r in raw]
    shortfall = total - sum(floored)
    remainders = sorted(
        range(len(raw)),
        key=lambda i: (raw[i] - floored[i], weights[i], -i),
        reverse=True,
    )
    steps = int((shortfall / quantum).to_integral_value())
    for k in range(steps):
        floored[remainders[k % len(raw)]] += quantum
    return floored


def build_net_plans(approved: list[ApprovedOrder]) -> list[NetPlan]:
    # Protective stops are EXCLUDED from netting, asserted rather than
    # encoded: a netted stop has no owning sleeve, and allocate_fill would
    # mis-attribute the exit — LedgerInvariantError territory. The paper cycle
    # filters them out before calling; this guard makes the contract loud.
    protective = [a for a in approved if a.intent.is_protective]
    if protective:
        raise ValueError(
            f"protective intents must never be netted: {[a.intent.intent_id for a in protective]}"
        )
    by_symbol: dict[str, list[ApprovedOrder]] = {}
    for order in approved:
        if order.intent.qty is None:
            continue  # notional flow is not netted in v1
        by_symbol.setdefault(order.intent.symbol, []).append(order)

    plans: list[NetPlan] = []
    for symbol in sorted(by_symbol):
        orders = by_symbol[symbol]
        asset_class = orders[0].intent.asset_class
        whole = asset_class is not AssetClass.CRYPTO
        legs = [_leg(o) for o in orders]
        buys = [leg for leg in legs if leg.side is Side.BUY]
        sells = [leg for leg in legs if leg.side is Side.SELL]
        buy_total = sum((leg.qty for leg in buys), Decimal("0"))
        sell_total = sum((leg.qty for leg in sells), Decimal("0"))
        cross_qty = min(buy_total, sell_total)

        cross_floor: Decimal | None = None
        cross_cap: Decimal | None = None
        if cross_qty > 0:
            buy_limits = [leg.limit_price for leg in buys if leg.limit_price is not None]
            sell_limits = [leg.limit_price for leg in sells if leg.limit_price is not None]
            cross_cap = min(buy_limits) if buy_limits else None
            cross_floor = max(sell_limits) if sell_limits else None
            if cross_cap is not None and cross_floor is not None and cross_cap < cross_floor:
                # No fair cross price exists: submit every leg individually.
                plans.append(
                    NetPlan(
                        symbol=symbol,
                        asset_class=asset_class,
                        residual_legs=(),
                        net_side=None,
                        net_qty=Decimal("0"),
                        net_limit=None,
                        crosses=(),
                        unnetted_legs=tuple(legs),
                    )
                )
                continue

        cross_fills: list[CrossFill] = []
        residual_by_leg: dict[str, Decimal] = {}
        for side_legs, side_total in ((buys, buy_total), (sells, sell_total)):
            if not side_legs:
                continue
            shares = _split_prorata(cross_qty, [leg.qty for leg in side_legs], whole)
            for leg, crossed in zip(side_legs, shares, strict=True):
                if crossed > 0:
                    cross_fills.append(
                        CrossFill(sleeve_id=leg.sleeve_id, side=leg.side, qty=crossed)
                    )
                residual_by_leg[leg.intent_id] = leg.qty - crossed

        net_qty = abs(buy_total - sell_total)
        net_side: Side | None
        if net_qty == 0:
            net_side = None
            residual_legs: tuple[Leg, ...] = ()
            net_limit = None
        else:
            net_side = Side.BUY if buy_total > sell_total else Side.SELL
            side_legs = buys if net_side is Side.BUY else sells
            residual_legs = tuple(
                leg.model_copy(update={"qty": residual_by_leg[leg.intent_id]})
                for leg in side_legs
                if residual_by_leg[leg.intent_id] > 0
            )
            limits = [leg.limit_price for leg in residual_legs if leg.limit_price is not None]
            if not limits:
                net_limit = None
            else:
                net_limit = min(limits) if net_side is Side.BUY else max(limits)

        plans.append(
            NetPlan(
                symbol=symbol,
                asset_class=asset_class,
                residual_legs=residual_legs,
                net_side=net_side,
                net_qty=net_qty,
                net_limit=net_limit,
                crosses=tuple(cross_fills),
                cross_floor=cross_floor,
                cross_cap=cross_cap,
            )
        )
    return plans


def cross_price(plan: NetPlan, bar_open: Decimal) -> Decimal:
    """Next-bar-open clamped into [cross_floor, cross_cap], so no crossing leg
    ever transacts outside its own limit. build_net_plans guarantees the
    interval is non-empty whenever crosses exist."""
    price = bar_open
    if plan.cross_cap is not None:
        price = min(price, plan.cross_cap)
    if plan.cross_floor is not None:
        price = max(price, plan.cross_floor)
    return price


def allocate_fill(
    plan: NetPlan, filled_qty: Decimal, total_fees: Decimal
) -> list[Allocation]:
    """Allocate an external fill on the residual order across net-side legs."""
    if not plan.residual_legs:
        return []
    whole = plan.asset_class is not AssetClass.CRYPTO
    weights = [leg.qty for leg in plan.residual_legs]
    quantities = _split_prorata(filled_qty, weights, whole)
    cent = Decimal("0.01")
    fee_shares = [
        (total_fees * q / filled_qty).quantize(cent, rounding=ROUND_HALF_EVEN)
        if filled_qty > 0
        else Decimal("0")
        for q in quantities
    ]
    fee_drift = total_fees - sum(fee_shares)
    if fee_drift != 0 and fee_shares:
        largest = max(range(len(quantities)), key=lambda i: (quantities[i], -i))
        fee_shares[largest] += fee_drift
    return [
        Allocation(sleeve_id=leg.sleeve_id, qty=q, fees=f)
        for leg, q, f in zip(plan.residual_legs, quantities, fee_shares, strict=True)
        if q > 0
    ]
