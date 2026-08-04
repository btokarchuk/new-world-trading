"""Alpaca Trading API v2, read-only except for one deliberate exception.

INVARIANT: cancel_all() is the only mutating request in the entire
nwt_watchdog package. It is a DELETE against /v2/orders and nothing else.
Every other call this package makes is a GET. A supervisor that can submit
orders, liquidate positions, or change account settings is just a second
trading system, and nothing supervises *it*; bounding the worst thing a
watchdog bug can do to "the open orders were cancelled" is what makes it safe
to run unattended. tests/test_monitor.py greps this package's source to keep
that true.

Re-implemented instead of importing the engine's adapter on purpose: the
watchdog exists to catch engine bugs, so it must not be able to inherit one.
Only the handful of response fields the invariants actually read are parsed.
"""

from datetime import datetime
from decimal import Decimal

import httpx

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"

_TIMEOUT_S = 10.0
# Our cadence is minutes-to-hours between calls, so a pooled connection is
# always idle long enough to go stale — and after a host suspend the peer has
# usually closed it while we still think it is live (observed 2026-08-04: an
# fd in CLOSE_WAIT and a scheduler wedged mid-request). Expiring keep-alives
# aggressively costs one handshake per call and buys a fresh socket every time.
_KEEPALIVE_EXPIRY_S = 15.0
_PAGE_LIMIT = 500


def parse_ts(value: str) -> datetime:
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


def _dec(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _order(payload: dict) -> dict:
    return {
        "id": str(payload.get("id", "")),
        "symbol": payload.get("symbol", ""),
        "created_at": parse_ts(payload["created_at"]),
    }


class AlpacaReadOnly:
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

    def account(self) -> dict:
        payload = self._get("/v2/account")
        return {
            "equity": _dec(payload.get("equity")),
            # last_equity is the previous session's close: equity - last_equity
            # is the broker's own view of today's P&L, so the watchdog never
            # has to reconstruct it from fills.
            "last_equity": _dec(payload.get("last_equity")),
            "cash": _dec(payload.get("cash")),
            "status": payload.get("status", ""),
        }

    def positions(self) -> list[dict]:
        return [
            {
                "symbol": position.get("symbol", ""),
                "qty": _dec(position.get("qty")),
                "market_value": _dec(position.get("market_value")),
            }
            for position in self._get("/v2/positions")
        ]

    def open_orders(self) -> list[dict]:
        payload = self._get("/v2/orders", {"status": "open", "limit": _PAGE_LIMIT})
        return [_order(order) for order in payload]

    def orders_since(self, since: datetime) -> list[dict]:
        payload = self._get(
            "/v2/orders",
            {"status": "all", "after": since.isoformat(), "limit": _PAGE_LIMIT},
        )
        return [_order(order) for order in payload]

    def cancel_all(self) -> dict:
        """The one mutating call in this package. 207 carries per-order results;
        a partial failure still leaves live orders, so both lists are returned
        and the caller reports them."""
        response = self._send("DELETE", "/v2/orders")
        response.raise_for_status()  # 207 is 2xx: per-item statuses parsed below
        try:
            items = response.json()
        except ValueError:
            items = []
        if not isinstance(items, list):
            items = []
        cancelled = [str(i.get("id", "")) for i in items if int(i.get("status", 0)) < 300]
        failed = [
            {"id": str(i.get("id", "")), "status": i.get("status"), "body": i.get("body")}
            for i in items
            if int(i.get("status", 0)) >= 300
        ]
        return {"http_status": response.status_code, "cancelled": cancelled, "failed": failed}

    def _get(self, path: str, params: dict | None = None):
        response = self._send("GET", path, params)
        response.raise_for_status()
        return response.json()

    def _send(self, method: str, path: str, params: dict | None = None) -> httpx.Response:
        return self._client.request(
            method, f"{self._base}{path}", headers=self._headers, params=params
        )
