import pytest

from nwt_engine.domain import IllegalOrderTransition, OrderState, assert_transition


def test_legal_lifecycle():
    path = [
        OrderState.INTENT,
        OrderState.APPROVED,
        OrderState.SUBMITTED,
        OrderState.ACKED,
        OrderState.PARTIAL,
        OrderState.FILLED,
    ]
    for current, new in zip(path, path[1:], strict=False):
        assert_transition(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (OrderState.FILLED, OrderState.CANCELED),
        (OrderState.REJECTED, OrderState.ACKED),
        (OrderState.INTENT, OrderState.FILLED),
        (OrderState.CANCELED, OrderState.PARTIAL),
    ],
)
def test_illegal_transitions_raise(current, new):
    with pytest.raises(IllegalOrderTransition):
        assert_transition(current, new)


class TestSimBrokerStops:
    """Stop geometry is separate from limits and deliberately pessimistic."""

    def _broker_with_stop(self, stop_price):
        from datetime import UTC, datetime
        from decimal import Decimal

        from nwt_contracts import Side
        from nwt_engine.broker.sim import SimBroker
        from nwt_engine.broker.sim.costs import CostModel
        from nwt_engine.domain import Instrument, OrderTicket, Universe

        universe = Universe(
            name="t",
            instruments=(Instrument(symbol="SPY", asset_class="etf", calendar="XNYS"),),
        )
        broker = SimBroker(
            universe,
            CostModel(),  # defaults; the stop path adds its own haircut
            Decimal("10000"),
            lambda: datetime(2026, 1, 2, tzinfo=UTC),
        )
        # hold 2 shares so the stop has something to sell
        broker._positions["SPY"] = (Decimal("2"), Decimal("700"))
        broker.submit(
            OrderTicket(
                client_order_id="prot-x",
                symbol="SPY",
                side=Side.SELL,
                qty=Decimal("2"),
                order_type="stop",
                stop_price=Decimal(stop_price),
                tif="gtc",
            )
        )
        return broker

    def _bar(self, o, h, lo, c, ts):
        from decimal import Decimal

        from nwt_engine.domain import Bar, Timeframe

        return Bar(
            symbol="SPY", timeframe=Timeframe.D1, ts_open=ts,
            ts_close=ts, open=Decimal(o), high=Decimal(h),
            low=Decimal(lo), close=Decimal(c), volume=Decimal("1000"),
        )

    def test_untouched_stop_rests_across_bars_and_sessions(self):
        from datetime import UTC, datetime

        broker = self._broker_with_stop("525")
        ts = datetime(2026, 1, 5, tzinfo=UTC)
        broker.on_bar(self._bar("700", "710", "690", "705", ts))
        broker.expire_day_orders()  # the close: GTC must survive
        assert len(broker.get_open_orders()) == 1
        assert broker.drain_events() == []

    def test_touched_stop_fills_pessimistically(self):
        from datetime import UTC, datetime
        from decimal import Decimal

        broker = self._broker_with_stop("525")
        ts = datetime(2026, 1, 5, tzinfo=UTC)
        # crash day: opens 600, prints 520 low — through the stop
        broker.on_bar(self._bar("600", "605", "520", "530", ts))
        fills = broker.drain_events()
        assert len(fills) == 1
        fill = fills[0]
        assert fill.protective is True
        # min(open 600, stop 525) = 525, minus the 100bps catastrophe haircut
        assert fill.price == Decimal("519.7500")

    def test_gap_through_fills_at_the_open_not_the_stop(self):
        from datetime import UTC, datetime
        from decimal import Decimal

        broker = self._broker_with_stop("525")
        ts = datetime(2026, 1, 5, tzinfo=UTC)
        # gaps DOWN through the level: opens 500 < stop 525
        broker.on_bar(self._bar("500", "510", "495", "505", ts))
        (fill,) = broker.drain_events()
        # min(open 500, stop 525) = 500, minus haircut => 495
        assert fill.price == Decimal("495.0000")
