"""FakeAlpaca: an in-process, stateful Alpaca Trading API v2 simulator.

Served through httpx.MockTransport so AlpacaHttpBroker runs its real request
code against scriptable broker truth. Fully deterministic: counter-derived ids
("fake-order-1", ...) and timestamps derived from a fixed base — chaos tests
replay identically.

Fault-injection knobs:
- fail_next(n, status=500): the next n requests (any endpoint) return status.
- timeout_next(n): the next n requests (any endpoint) raise TimeoutException.
- timeout_but_create_next(): the next order POST records the order and THEN
  raises a timeout — the ambiguous-create case the submit protocol exists for.
- reject_next(reason): the next order POST returns 422 with that reason.

`positions` is a plain mutable dict on purpose: tests tamper with it directly
to simulate broker-side drift the ledger never saw.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

_OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled"}
_BASE_TS = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)


class FakeAlpaca:
    def __init__(self, cash: Decimal | str = "10000", base_ts: datetime = _BASE_TS) -> None:
        self.cash = Decimal(cash)
        self.equity_override: Decimal | None = None
        # symbol -> {"qty": Decimal, "avg_entry_price": Decimal}
        self.positions: dict[str, dict[str, Decimal]] = {}
        self.orders: list[dict] = []
        self.requests: list[httpx.Request] = []
        self._base_ts = base_ts
        self._order_seq = 0
        self._ext_seq = 0
        self._ts_seq = 0
        self._fail_queue: list[int] = []
        self._timeout_remaining = 0
        self._timeout_but_create_remaining = 0
        self._reject_queue: list[str] = []

    # -- wiring --------------------------------------------------------------

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    @property
    def equity(self) -> Decimal:
        if self.equity_override is not None:
            return self.equity_override
        held = sum(
            (p["qty"] * p["avg_entry_price"] for p in self.positions.values()), Decimal("0")
        )
        return self.cash + held

    # -- fault injection -----------------------------------------------------

    def fail_next(self, n: int, status: int = 500) -> None:
        self._fail_queue.extend([status] * n)

    def timeout_next(self, n: int) -> None:
        self._timeout_remaining += n

    def timeout_but_create_next(self) -> None:
        self._timeout_but_create_remaining += 1

    def reject_next(self, reason: str) -> None:
        self._reject_queue.append(reason)

    # -- manual lifecycle ----------------------------------------------------

    def fill(self, order_id: str, price: Decimal | str) -> None:
        order = self._order_by_id(order_id)
        if order is None:
            raise KeyError(order_id)
        if order["status"] not in _OPEN_STATUSES:
            raise ValueError(f"{order_id} is {order['status']}, not fillable")
        price = Decimal(str(price))
        if order["qty"] is not None:
            qty = Decimal(order["qty"])
        else:
            qty = Decimal(order["notional"]) / price
        order["status"] = "filled"
        order["filled_qty"] = str(qty)
        order["filled_avg_price"] = str(price)
        order["filled_at"] = self._next_ts()
        symbol = order["symbol"]
        pos = self.positions.get(
            symbol, {"qty": Decimal("0"), "avg_entry_price": Decimal("0")}
        )
        if order["side"] == "buy":
            new_qty = pos["qty"] + qty
            pos["avg_entry_price"] = (
                pos["qty"] * pos["avg_entry_price"] + qty * price
            ) / new_qty
            pos["qty"] = new_qty
            self.cash -= qty * price
        else:
            pos["qty"] -= qty
            self.cash += qty * price
        self.positions[symbol] = pos

    def create_external(
        self,
        symbol: str,
        side: str = "buy",
        qty: Decimal | str | None = None,
        notional: Decimal | str | None = None,
        limit_price: Decimal | str | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """An order placed at the broker outside the adapter (web UI, etc.)."""
        self._ext_seq += 1
        coid = client_order_id or f"ext-{self._ext_seq}"
        order = self._new_order(
            symbol=symbol,
            side=side,
            qty=None if qty is None else str(qty),
            notional=None if notional is None else str(notional),
            limit_price=None if limit_price is None else str(limit_price),
            client_order_id=coid,
        )
        self.orders.append(order)
        return order

    # -- transport handler ---------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._timeout_remaining > 0:
            self._timeout_remaining -= 1
            raise httpx.TimeoutException("injected timeout", request=request)
        if self._fail_queue:
            return httpx.Response(self._fail_queue.pop(0), json={"message": "injected failure"})
        method, path = request.method, request.url.path
        if method == "POST" and path == "/v2/orders":
            return self._create_order(request)
        if method == "GET" and path == "/v2/orders:by_client_order_id":
            return self._lookup(request)
        if method == "GET" and path == "/v2/orders":
            return self._list_orders(request)
        if method == "DELETE" and path == "/v2/orders":
            return self._cancel_all()
        if method == "DELETE" and path.startswith("/v2/orders/"):
            return self._cancel_one(path.rsplit("/", 1)[1])
        if method == "GET" and path == "/v2/positions":
            return self._list_positions()
        if method == "DELETE" and path == "/v2/positions":
            return self._close_all_positions(request)
        if method == "GET" and path == "/v2/account":
            return httpx.Response(
                200, json={"cash": str(self.cash), "equity": str(self.equity)}
            )
        if method == "GET" and path == "/v2/clock":
            return httpx.Response(
                200,
                json={
                    "timestamp": self._base_ts.isoformat(),
                    "is_open": True,
                    "next_open": (self._base_ts + timedelta(days=1)).isoformat(),
                    "next_close": (self._base_ts + timedelta(hours=6)).isoformat(),
                },
            )
        return httpx.Response(404, json={"message": f"unhandled route {method} {path}"})

    # -- routes --------------------------------------------------------------

    def _create_order(self, request: httpx.Request) -> httpx.Response:
        if self._reject_queue:
            return httpx.Response(
                422, json={"code": 42210000, "message": self._reject_queue.pop(0)}
            )
        body = json.loads(request.content)
        coid = body["client_order_id"]
        if any(o["client_order_id"] == coid for o in self.orders):
            # Alpaca's exact dedupe behavior for a reused client_order_id.
            return httpx.Response(
                422, json={"code": 40010001, "message": "client_order_id must be unique"}
            )
        order = self._new_order(
            symbol=body["symbol"],
            side=body["side"],
            qty=body.get("qty"),
            notional=body.get("notional"),
            limit_price=body.get("limit_price"),
            client_order_id=coid,
        )
        self.orders.append(order)
        if self._timeout_but_create_remaining > 0:
            self._timeout_but_create_remaining -= 1
            raise httpx.TimeoutException("injected timeout after create", request=request)
        return httpx.Response(200, json=order)

    def _lookup(self, request: httpx.Request) -> httpx.Response:
        coid = request.url.params["client_order_id"]
        for order in reversed(self.orders):
            if order["client_order_id"] == coid:
                return httpx.Response(200, json=order)
        return httpx.Response(404, json={"code": 40410000, "message": "order not found"})

    def _list_orders(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        status = params.get("status", "all")
        after = params.get("after")
        limit = int(params.get("limit", "500"))
        selected = []
        for order in self.orders:
            is_open = order["status"] in _OPEN_STATUSES
            if status == "open" and not is_open:
                continue
            if status == "closed" and is_open:
                continue
            if after is not None and order["submitted_at"] <= after:
                continue
            selected.append(order)
        return httpx.Response(200, json=selected[:limit])

    def _cancel_all(self) -> httpx.Response:
        results = []
        for order in self.orders:
            if order["status"] in _OPEN_STATUSES:
                order["status"] = "canceled"
                results.append({"id": order["id"], "status": 200})
        return httpx.Response(207, json=results)

    def _cancel_one(self, order_id: str) -> httpx.Response:
        order = self._order_by_id(order_id)
        if order is None or order["status"] not in _OPEN_STATUSES:
            # Matches Alpaca: canceling a terminal/unknown order is a 404.
            return httpx.Response(404, json={"message": "order not found"})
        order["status"] = "canceled"
        return httpx.Response(204)

    def _list_positions(self) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": symbol,
                    "asset_class": "crypto" if "/" in symbol else "us_equity",
                    "qty": str(pos["qty"]),
                    "avg_entry_price": str(pos["avg_entry_price"]),
                }
                for symbol, pos in self.positions.items()
                if pos["qty"] != 0
            ],
        )

    def _close_all_positions(self, request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cancel_orders") == "true":
            self._cancel_all()
        results = []
        for symbol, pos in self.positions.items():
            if pos["qty"] == 0:
                continue
            results.append(
                {"symbol": symbol, "status": 200, "body": {"symbol": symbol, "status": "accepted"}}
            )
            pos["qty"] = Decimal("0")
        return httpx.Response(207, json=results)

    # -- internals -----------------------------------------------------------

    def _order_by_id(self, order_id: str) -> dict | None:
        return next((o for o in self.orders if o["id"] == order_id), None)

    def _next_ts(self) -> str:
        self._ts_seq += 1
        return (self._base_ts + timedelta(seconds=self._ts_seq)).isoformat()

    def _new_order(
        self,
        symbol: str,
        side: str,
        qty: str | None,
        notional: str | None,
        limit_price: str | None,
        client_order_id: str,
    ) -> dict:
        self._order_seq += 1
        ts = self._next_ts()
        return {
            "id": f"fake-order-{self._order_seq}",
            "client_order_id": client_order_id,
            "status": "accepted",
            "symbol": symbol,
            "side": side,
            "asset_class": "crypto" if "/" in symbol else "us_equity",
            "qty": qty,
            "notional": notional,
            "limit_price": limit_price,
            "created_at": ts,
            "submitted_at": ts,
            "filled_qty": "0",
            "filled_avg_price": None,
            "filled_at": None,
        }
