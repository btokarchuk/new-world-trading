"""Alpaca Trading API v2 adapter over raw httpx.

Deliberate deviation from the official alpaca-py SDK: raw httpx with an
injectable Client means every failure mode we care about — timeouts, 5xx,
403/422 rejects, 207 multi-status — can be scripted through
httpx.MockTransport (chaos-testability). The SDK owns its transport and
retry policy; this adapter owns them explicitly because submit-ambiguity
handling IS the product.

Submit ambiguity protocol: on timeout/5xx the order may or may not exist at
the broker, so we never blind-retry. First look the order up by
client_order_id; only a confirmed 404 earns exactly one retried POST. A
second ambiguity is surrendered to the reconcile engine as
state=SUBMITTED / "ambiguous — reconcile required".

Fees: the closed-order poll used by drain_events carries no fee data, so
fills report fees=0; fee truth arrives via reconcile/account activities in
Phase 4.
"""

import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from nwt_contracts import Side

from nwt_engine.domain import Fill, OrderState, OrderTicket

from ..base import AccountState, Broker, BrokerPosition, OrderAck, OrderStatus

_TIMEOUT_S = 10.0
# Our cadence is minutes-to-hours between calls, so a pooled connection is
# always idle long enough to go stale — and after a host suspend the peer has
# usually closed it while we still think it is live (observed 2026-08-04: an
# fd in CLOSE_WAIT and a scheduler wedged mid-request). Expiring keep-alives
# aggressively costs one handshake per call and buys a fresh socket every time.
_KEEPALIVE_EXPIRY_S = 15.0
_RETRY_BACKOFF_S = 0.5
# Longest suffixes first so ETHUSDT never mis-splits as ETHUS/DT via USD.
_CRYPTO_QUOTES = ("USDT", "USDC", "USD")
_AMBIGUOUS = object()

_STATE_MAP: dict[str, OrderState] = {
    "new": OrderState.ACKED,
    "accepted": OrderState.ACKED,
    "pending_new": OrderState.ACKED,
    "partially_filled": OrderState.PARTIAL,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELED,
    "replaced": OrderState.CANCELED,
    "expired": OrderState.EXPIRED,
    "rejected": OrderState.REJECTED,
}


def to_domain_symbol(symbol: str, asset_class: str) -> str:
    """Alpaca's positions endpoint reports crypto as 'BTCUSD'; our domain uses
    'BTC/USD'. Equities pass through unchanged (asset_class guards tickers that
    merely end in a quote-currency string)."""
    if asset_class != "crypto" or "/" in symbol:
        return symbol
    for quote in _CRYPTO_QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[: -len(quote)]}/{quote}"
    return symbol


def _order_state(raw_status: str) -> OrderState:
    # Unlisted intermediate statuses (pending_cancel, done_for_day, held, ...)
    # are still live at the broker: ACKED, with reconcile as the backstop.
    return _STATE_MAP.get(raw_status, OrderState.ACKED)


def _parse_ts(value: str) -> datetime:
    """RFC3339 with up to nanosecond precision; fromisoformat caps at micros."""
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    head, dot, rest = value.partition(".")
    if dot:
        index = 0
        while index < len(rest) and rest[index].isdigit():
            index += 1
        value = f"{head}.{rest[:index][:6]}{rest[index:]}"
    return datetime.fromisoformat(value)


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict) and "message" in payload:
        return str(payload["message"])
    return response.text


class BrokerError(RuntimeError):
    """A broker operation failed in a way the caller must not ignore."""


class AlpacaHttpBroker(Broker):
    def __init__(
        self,
        base_url: str,
        key_id: str,
        secret: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self._client = client or httpx.Client(
            timeout=_TIMEOUT_S,
            limits=httpx.Limits(keepalive_expiry=_KEEPALIVE_EXPIRY_S),
        )
        self._events_cursor: str | None = None
        # (broker order id, filled_qty) already surfaced by drain_events.
        # In-memory only by design: the reconcile engine is the durable backstop.
        self._reported: set[tuple[str, str]] = set()

    # -- Broker interface ----------------------------------------------------

    def submit(self, ticket: OrderTicket) -> OrderAck:
        body = self._order_body(ticket)
        first = self._post_once(ticket, body)
        if first is not None:
            return first
        # Ambiguous POST: never blind-retry; ask the broker what it saw.
        looked_up = self._lookup(ticket.client_order_id)
        if looked_up is _AMBIGUOUS:
            return self._ambiguous_ack(ticket)
        if looked_up is not None:
            return self._ack_from_order(looked_up)
        # Confirmed 404: the POST never landed, so one retry is safe.
        time.sleep(_RETRY_BACKOFF_S)
        second = self._post_once(ticket, body)
        if second is not None:
            return second
        return self._ambiguous_ack(ticket)

    def cancel(self, client_order_id: str) -> None:
        response = self._request(
            "GET",
            "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
        )
        if response.status_code == 404:
            return  # already gone — idempotent, matching SimBroker.cancel
        response.raise_for_status()
        deletion = self._request("DELETE", f"/v2/orders/{response.json()['id']}")
        if deletion.status_code != 404:  # 404 = reached terminal state in the race
            deletion.raise_for_status()

    def cancel_all(self) -> list[dict]:
        """Cancel everything; report per-order outcomes.

        DELETE /v2/orders returns 207 multi-status. 207 is 2xx, so
        raise_for_status() alone lets per-item failures ("order no longer
        cancelable") pass silently — and once protective stops rest, the kill
        switch must be able to say whether the stops actually died. Returns
        the per-order status list; entries with status >= 300 failed.
        """
        response = self._request("DELETE", "/v2/orders")
        response.raise_for_status()
        try:
            items = response.json()
        except ValueError:
            return []
        failures = [i for i in items if int(i.get("status", 500)) >= 300]
        if failures:
            ids = ", ".join(str(i.get("id", "?")) for i in failures[:5])
            raise BrokerError(
                f"cancel_all: {len(failures)}/{len(items)} cancels failed ({ids})"
            )
        return items

    def get_open_orders(self) -> list[OrderStatus]:
        response = self._request("GET", "/v2/orders", params={"status": "open", "limit": 500})
        response.raise_for_status()
        return [
            OrderStatus(
                client_order_id=order["client_order_id"],
                state=_order_state(order["status"]),
                filled_qty=Decimal(order.get("filled_qty") or "0"),
                ts=_parse_ts(order.get("submitted_at") or order["created_at"]),
            )
            for order in response.json()
        ]

    def get_positions(self) -> list[BrokerPosition]:
        response = self._request("GET", "/v2/positions")
        response.raise_for_status()
        return [
            BrokerPosition(
                symbol=to_domain_symbol(position["symbol"], position.get("asset_class", "")),
                qty=Decimal(position["qty"]),
                avg_cost=Decimal(position["avg_entry_price"]),
            )
            for position in response.json()
        ]

    def get_account(self) -> AccountState:
        response = self._request("GET", "/v2/account")
        response.raise_for_status()
        payload = response.json()
        return AccountState(
            ts=self._now(), cash=Decimal(payload["cash"]), equity=Decimal(payload["equity"])
        )

    def drain_events(self) -> list[Fill]:
        """v1 attended-cycle model: poll closed orders past the cursor and
        synthesize one Fill per (order, filled_qty) not yet reported."""
        params: dict = {"status": "closed", "limit": 500}
        if self._events_cursor is not None:
            params["after"] = self._events_cursor
        response = self._request("GET", "/v2/orders", params=params)
        response.raise_for_status()
        fills: list[Fill] = []
        for order in response.json():
            submitted_at = order.get("submitted_at") or order["created_at"]
            if self._events_cursor is None or submitted_at > self._events_cursor:
                self._events_cursor = submitted_at
            filled_qty = order.get("filled_qty") or "0"
            if Decimal(filled_qty) <= 0:
                continue
            key = (order["id"], filled_qty)
            if key in self._reported:
                continue
            self._reported.add(key)
            fills.append(
                Fill(
                    fill_id=f"alpaca-{order['id']}",
                    client_order_id=order["client_order_id"],
                    symbol=to_domain_symbol(order["symbol"], order.get("asset_class", "")),
                    side=Side(order["side"]),
                    qty=Decimal(filled_qty),
                    price=Decimal(order["filled_avg_price"]),
                    ts=_parse_ts(order["filled_at"]),
                    fees=Decimal("0"),  # no fee data here; Phase 4 activities own fee truth
                    source="broker",
                )
            )
        return fills

    # -- extras (kill CLI / session gating) ----------------------------------

    def clock(self) -> dict:
        response = self._request("GET", "/v2/clock")
        response.raise_for_status()
        payload = response.json()
        return {
            "timestamp": _parse_ts(payload["timestamp"]),
            "is_open": bool(payload["is_open"]),
            "next_open": _parse_ts(payload["next_open"]),
            "next_close": _parse_ts(payload["next_close"]),
        }

    def close_all_positions(self, cancel_orders: bool) -> list[dict]:
        response = self._request(
            "DELETE",
            "/v2/positions",
            params={"cancel_orders": "true" if cancel_orders else "false"},
        )
        response.raise_for_status()  # 207 is 2xx: per-item statuses parsed below
        return [
            {"symbol": item.get("symbol"), "status": item.get("status"), "body": item.get("body")}
            for item in response.json()
        ]

    # -- internals -----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self._client.request(method, f"{self._base}{path}", headers=self._headers, **kwargs)

    @staticmethod
    def _now() -> datetime:
        # Wall clock only at the transport boundary, for acks with no broker
        # timestamp to derive from (timeouts, error bodies) — never decisions.
        return datetime.now(UTC)

    @staticmethod
    def _order_body(ticket: OrderTicket) -> dict[str, str]:
        body = {
            "symbol": ticket.symbol,  # orders API accepts the slashed crypto form as-is
            "side": ticket.side.value,
            "type": "limit" if ticket.limit_price is not None else "market",
            "time_in_force": ticket.tif,
            "client_order_id": ticket.client_order_id,
        }
        if ticket.qty is not None:
            body["qty"] = str(ticket.qty)
        else:
            body["notional"] = str(ticket.notional)
        if ticket.limit_price is not None:
            body["limit_price"] = str(ticket.limit_price)
        return body

    def _ack_from_order(self, order: dict) -> OrderAck:
        return OrderAck(
            client_order_id=order["client_order_id"],
            state=_order_state(order["status"]),
            ts=_parse_ts(order.get("submitted_at") or order["created_at"]),
        )

    def _ambiguous_ack(self, ticket: OrderTicket) -> OrderAck:
        return OrderAck(
            client_order_id=ticket.client_order_id,
            state=OrderState.SUBMITTED,
            ts=self._now(),
            reason="ambiguous — reconcile required",
        )

    def _post_once(self, ticket: OrderTicket, body: dict[str, str]) -> OrderAck | None:
        """One POST attempt. None means the outcome is unknown (timeout/5xx)."""
        try:
            response = self._request("POST", "/v2/orders", json=body)
        except httpx.TimeoutException:
            return None
        if response.status_code >= 500:
            return None
        if response.status_code == 422:
            return OrderAck(
                client_order_id=ticket.client_order_id,
                state=OrderState.REJECTED,
                ts=self._now(),
                reason=_error_message(response),
            )
        if response.status_code == 403:
            message = _error_message(response)
            lowered = message.lower()
            if "buying power" in lowered or "wash trade" in lowered:
                return OrderAck(
                    client_order_id=ticket.client_order_id,
                    state=OrderState.REJECTED,
                    ts=self._now(),
                    reason=message,
                )
            response.raise_for_status()  # genuine auth/permission failure: loud
        response.raise_for_status()
        return self._ack_from_order(response.json())

    def _lookup(self, client_order_id: str):
        """GET by client_order_id: order dict, None on confirmed 404, or the
        _AMBIGUOUS sentinel when the lookup itself failed."""
        try:
            response = self._request(
                "GET",
                "/v2/orders:by_client_order_id",
                params={"client_order_id": client_order_id},
            )
        except httpx.TimeoutException:
            return _AMBIGUOUS
        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            return _AMBIGUOUS
        response.raise_for_status()
        return response.json()
