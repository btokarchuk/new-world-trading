import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from nwt_contracts import Side

from nwt_engine.broker.alpaca import AlpacaHttpBroker
from nwt_engine.broker.alpaca.http_broker import to_domain_symbol
from nwt_engine.domain import OrderState, OrderTicket

BASE = "https://paper-api.alpaca.markets"


def _broker(handler) -> tuple[AlpacaHttpBroker, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    return AlpacaHttpBroker(BASE, "key", "secret", client=client), requests


def _order_json(coid: str = "coid-1", status: str = "accepted", **extra) -> dict:
    base = {
        "id": "oid-1",
        "client_order_id": coid,
        "status": status,
        "symbol": "AAPL",
        "side": "buy",
        "asset_class": "us_equity",
        "created_at": "2026-07-20T13:30:00.123456789Z",
        "submitted_at": "2026-07-20T13:30:00.123456789Z",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    base.update(extra)
    return base


def _ticket(**overrides) -> OrderTicket:
    fields = dict(
        client_order_id="coid-1",
        symbol="AAPL",
        side=Side.BUY,
        qty=Decimal("10"),
        limit_price=Decimal("189.50"),
    )
    fields.update(overrides)
    return OrderTicket(**fields)


def _no_sleep(monkeypatch) -> None:
    monkeypatch.setattr("nwt_engine.broker.alpaca.http_broker.time.sleep", lambda _: None)


# -- submit ------------------------------------------------------------------


def test_submit_happy_path_exact_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_order_json())

    broker, requests = _broker(handler)
    ack = broker.submit(_ticket())

    (request,) = requests
    assert request.method == "POST"
    assert request.url.path == "/v2/orders"
    assert request.headers["APCA-API-KEY-ID"] == "key"
    assert request.headers["APCA-API-SECRET-KEY"] == "secret"
    assert json.loads(request.content) == {
        "symbol": "AAPL",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "client_order_id": "coid-1",
        "qty": "10",
        "limit_price": "189.50",
    }
    assert ack.client_order_id == "coid-1"
    assert ack.state is OrderState.ACKED
    assert ack.ts == datetime(2026, 7, 20, 13, 30, 0, 123456, tzinfo=UTC)
    assert ack.reason is None


def test_timeout_then_lookup_found_returns_original_ack():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ReadTimeout("boom", request=request)
        assert request.url.path == "/v2/orders:by_client_order_id"
        assert request.url.params["client_order_id"] == "coid-1"
        return httpx.Response(200, json=_order_json(status="new"))

    broker, requests = _broker(handler)
    ack = broker.submit(_ticket())

    assert ack.state is OrderState.ACKED
    assert sum(1 for r in requests if r.method == "POST") == 1  # never blind-retried


def test_timeout_then_404_retries_post_exactly_once(monkeypatch):
    _no_sleep(monkeypatch)
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "POST":
            post_count += 1
            if post_count == 1:
                raise httpx.ConnectTimeout("boom", request=request)
            return httpx.Response(200, json=_order_json(status="new"))
        return httpx.Response(404, json={"code": 40410000, "message": "order not found"})

    broker, requests = _broker(handler)
    ack = broker.submit(_ticket())

    assert ack.state is OrderState.ACKED
    assert post_count == 2
    assert sum(1 for r in requests if r.method == "POST") == 2


def test_double_ambiguity_returns_submitted_for_reconcile(monkeypatch):
    _no_sleep(monkeypatch)
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "POST":
            post_count += 1
            if post_count == 1:
                raise httpx.ReadTimeout("boom", request=request)
            return httpx.Response(503, text="upstream unavailable")  # 5xx = ambiguous too
        return httpx.Response(404, json={"message": "order not found"})

    broker, _ = _broker(handler)
    ack = broker.submit(_ticket())

    assert ack.state is OrderState.SUBMITTED
    assert ack.reason is not None and "ambiguous" in ack.reason
    assert post_count == 2  # never a third attempt


def test_403_buying_power_maps_to_rejected_with_broker_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": 40310000, "message": "insufficient buying power"})

    broker, _ = _broker(handler)
    ack = broker.submit(_ticket())

    assert ack.state is OrderState.REJECTED
    assert ack.reason == "insufficient buying power"


def test_422_maps_to_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"code": 42210000, "message": "invalid limit_price"})

    broker, _ = _broker(handler)
    ack = broker.submit(_ticket())

    assert ack.state is OrderState.REJECTED
    assert ack.reason == "invalid limit_price"


# -- symbol normalization ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "asset_class", "expected"),
    [
        ("BTCUSD", "crypto", "BTC/USD"),
        ("ETHUSDT", "crypto", "ETH/USDT"),
        ("SOLUSDC", "crypto", "SOL/USDC"),
        ("BTC/USD", "crypto", "BTC/USD"),  # already-slashed passes through
        ("AAPL", "us_equity", "AAPL"),
        ("TUSD", "us_equity", "TUSD"),  # equity ticker ending in USD stays intact
    ],
)
def test_to_domain_symbol_mapping(raw, asset_class, expected):
    assert to_domain_symbol(raw, asset_class) == expected


def test_crypto_order_keeps_slashed_symbol_on_wire():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_order_json(coid="c-2", symbol="BTC/USD", asset_class="crypto")
        )

    broker, requests = _broker(handler)
    ack = broker.submit(
        OrderTicket(
            client_order_id="c-2", symbol="BTC/USD", side=Side.BUY, notional=Decimal("100")
        )
    )

    assert json.loads(requests[0].content) == {
        "symbol": "BTC/USD",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": "c-2",
        "notional": "100",
    }
    assert ack.state is OrderState.ACKED


def test_positions_read_normalizes_crypto_only():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/positions"
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSD",
                    "asset_class": "crypto",
                    "qty": "0.5",
                    "avg_entry_price": "42000.50",
                },
                {
                    "symbol": "AAPL",
                    "asset_class": "us_equity",
                    "qty": "10",
                    "avg_entry_price": "190.10",
                },
            ],
        )

    broker, _ = _broker(handler)
    positions = broker.get_positions()

    assert [(p.symbol, p.qty, p.avg_cost) for p in positions] == [
        ("BTC/USD", Decimal("0.5"), Decimal("42000.50")),
        ("AAPL", Decimal("10"), Decimal("190.10")),
    ]


# -- drain_events ------------------------------------------------------------


def test_drain_events_reports_each_fill_exactly_once_across_polls():
    filled_1 = _order_json(
        coid="c-1",
        status="filled",
        id="oid-1",
        filled_qty="10",
        filled_avg_price="50.25",
        filled_at="2026-07-20T14:00:00Z",
        submitted_at="2026-07-20T13:59:00Z",
    )
    canceled = _order_json(
        coid="c-2", status="canceled", id="oid-2", submitted_at="2026-07-20T13:58:00Z"
    )
    filled_2 = _order_json(
        coid="c-3",
        status="filled",
        id="oid-3",
        filled_qty="2",
        filled_avg_price="99.10",
        filled_at="2026-07-20T14:05:00Z",
        submitted_at="2026-07-20T14:04:00Z",
    )
    polls = iter([[filled_1, canceled], [filled_1, filled_2]])
    seen_params: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json=next(polls))

    broker, _ = _broker(handler)

    first = broker.drain_events()
    assert [f.client_order_id for f in first] == ["c-1"]  # zero-fill canceled skipped
    (fill,) = first
    assert fill.fill_id == "alpaca-oid-1"
    assert fill.side is Side.BUY
    assert fill.qty == Decimal("10")
    assert fill.price == Decimal("50.25")
    assert fill.ts == datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    assert fill.fees == Decimal("0")  # fee truth arrives via reconcile in Phase 4
    assert fill.source == "broker"
    assert seen_params[0]["status"] == "closed"
    assert "after" not in seen_params[0]

    second = broker.drain_events()
    assert [f.client_order_id for f in second] == ["c-3"]  # c-1 not re-reported
    assert seen_params[1]["after"] == "2026-07-20T13:59:00Z"  # cursor = max submitted_at


# -- cancel / close / clock / reads ------------------------------------------


def test_cancel_looks_up_broker_id_then_deletes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_order_json(id="oid-9"))
        assert request.method == "DELETE"
        assert request.url.path == "/v2/orders/oid-9"
        return httpx.Response(204)

    broker, requests = _broker(handler)
    broker.cancel("coid-1")
    assert [r.method for r in requests] == ["GET", "DELETE"]


def test_cancel_all_deletes_orders_collection():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v2/orders"
        return httpx.Response(207, json=[{"id": "oid-1", "status": 200}])

    broker, requests = _broker(handler)
    broker.cancel_all()
    assert len(requests) == 1


def test_close_all_positions_parses_207_per_item_results():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v2/positions"
        assert request.url.params["cancel_orders"] == "true"
        return httpx.Response(
            207,
            json=[
                {"symbol": "AAPL", "status": 200, "body": {"id": "oid-1", "status": "accepted"}},
                {"symbol": "BTCUSD", "status": 422, "body": {"message": "cannot close"}},
            ],
        )

    broker, _ = _broker(handler)
    results = broker.close_all_positions(cancel_orders=True)
    assert results == [
        {"symbol": "AAPL", "status": 200, "body": {"id": "oid-1", "status": "accepted"}},
        {"symbol": "BTCUSD", "status": 422, "body": {"message": "cannot close"}},
    ]


def test_clock_parsing_trims_nanoseconds():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/clock"
        return httpx.Response(
            200,
            json={
                "timestamp": "2026-07-20T09:31:15.123456789-04:00",
                "is_open": True,
                "next_open": "2026-07-21T09:30:00-04:00",
                "next_close": "2026-07-20T16:00:00-04:00",
            },
        )

    broker, _ = _broker(handler)
    clock = broker.clock()

    eastern = timezone(timedelta(hours=-4))
    assert clock["is_open"] is True
    assert clock["timestamp"] == datetime(2026, 7, 20, 9, 31, 15, 123456, tzinfo=eastern)
    assert clock["next_open"] == datetime(2026, 7, 21, 9, 30, tzinfo=eastern)
    assert clock["next_close"] == datetime(2026, 7, 20, 16, 0, tzinfo=eastern)


def test_open_orders_and_account_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/orders":
            assert request.url.params["status"] == "open"
            assert request.url.params["limit"] == "500"
            return httpx.Response(
                200, json=[_order_json(status="partially_filled", filled_qty="3")]
            )
        assert request.url.path == "/v2/account"
        return httpx.Response(200, json={"cash": "9450.10", "equity": "10012.55"})

    broker, _ = _broker(handler)

    (status,) = broker.get_open_orders()
    assert status.client_order_id == "coid-1"
    assert status.state is OrderState.PARTIAL
    assert status.filled_qty == Decimal("3")

    account = broker.get_account()
    assert account.cash == Decimal("9450.10")
    assert account.equity == Decimal("10012.55")
