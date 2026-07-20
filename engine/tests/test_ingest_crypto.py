from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from nwt_engine.data import ParquetStore
from nwt_engine.data.ingest import fetch_crypto_daily_bars, ingest_crypto
from nwt_engine.domain import Timeframe

START = date(2024, 1, 1)
END = date(2024, 1, 5)


def _raw(t: str, px: float, vol: float = 100.5) -> dict:
    return {"t": t, "o": px, "h": px + 1, "l": px - 1, "c": px + 0.5, "v": vol}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pagination_two_pages():
    requests: list[httpx.Request] = []
    pages = {
        None: {
            "bars": {"BTC/USD": [_raw("2024-01-02T06:00:00Z", 43000.0)]},
            "next_page_token": "tok",
        },
        "tok": {
            "bars": {"BTC/USD": [_raw("2024-01-01T06:00:00Z", 42000.0)]},
            "next_page_token": None,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[request.url.params.get("page_token")])

    bars = fetch_crypto_daily_bars(["BTC/USD"], START, END, client=_client(handler))

    assert len(requests) == 2
    assert requests[0].url.params["symbols"] == "BTC/USD"
    assert requests[0].url.params["timeframe"] == "1Day"
    assert requests[0].url.params["start"] == "2024-01-01T00:00:00Z"
    assert requests[1].url.params["page_token"] == "tok"
    # merged across pages and sorted by ts even though page 2 was earlier
    assert [b.ts_open.day for b in bars["BTC/USD"]] == [1, 2]


def test_decimal_conversion_is_exact():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "bars": {
                    "BTC/USD": [
                        {
                            "t": "2024-01-01T06:00:00Z",
                            "o": 0.1,
                            "h": 68123.45,
                            "l": 0.07,
                            "c": 42999.99,
                            "v": 12345.678,
                        }
                    ]
                },
                "next_page_token": None,
            },
        )

    (bar,) = fetch_crypto_daily_bars(["BTC/USD"], START, END, client=_client(handler))["BTC/USD"]
    assert bar.open == Decimal("0.1")
    assert bar.high == Decimal("68123.45")
    assert bar.low == Decimal("0.07")
    assert bar.close == Decimal("42999.99")
    assert bar.volume == Decimal("12345.678")


def test_ts_open_close_derivation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "bars": {"ETH/USD": [_raw("2024-01-03T05:00:00Z", 2200.0)]},
                "next_page_token": None,
            },
        )

    (bar,) = fetch_crypto_daily_bars(["ETH/USD"], START, END, client=_client(handler))["ETH/USD"]
    assert bar.ts_open == datetime(2024, 1, 3, 5, tzinfo=UTC)
    assert bar.ts_close == bar.ts_open + timedelta(days=1)
    assert bar.timeframe is Timeframe.D1


def test_empty_symbol_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bars": {}, "next_page_token": None})

    bars = fetch_crypto_daily_bars(["BTC/USD", "ETH/USD"], START, END, client=_client(handler))
    assert bars == {"BTC/USD": [], "ETH/USD": []}


def test_auth_error_raises_clear_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(RuntimeError, match="keys"):
        fetch_crypto_daily_bars(["BTC/USD"], START, END, client=_client(handler))


def test_retries_once_on_429(monkeypatch):
    monkeypatch.setattr("nwt_engine.data.ingest.alpaca_crypto.time.sleep", lambda _: None)
    statuses = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(
            200,
            json={
                "bars": {"BTC/USD": [_raw("2024-01-01T06:00:00Z", 42000.0)]},
                "next_page_token": None,
            },
        )

    bars = fetch_crypto_daily_bars(["BTC/USD"], START, END, client=_client(handler))
    assert len(bars["BTC/USD"]) == 1


def test_ingest_writes_alpaca_provider_and_round_trips(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "bars": {
                    "BTC/USD": [
                        _raw("2024-01-01T06:00:00Z", 42000.1),
                        _raw("2024-01-02T06:00:00Z", 43000.2),
                    ],
                    "ETH/USD": [_raw("2024-01-01T06:00:00Z", 2200.3)],
                },
                "next_page_token": None,
            },
        )

    store = ParquetStore(tmp_path)
    counts = ingest_crypto(store, ["BTC/USD", "ETH/USD"], START, END, client=_client(handler))

    assert counts == {"BTC/USD": 2, "ETH/USD": 1}
    round_tripped = store.read_bars("alpaca", Timeframe.D1, "BTC/USD")
    assert [b.open for b in round_tripped] == [Decimal("42000.1"), Decimal("43000.2")]


@pytest.mark.network
def test_live_fetch_btc_smoke():
    end = date.today()
    bars = fetch_crypto_daily_bars(["BTC/USD"], end - timedelta(days=5), end)["BTC/USD"]
    assert 4 <= len(bars) <= 7
    assert all(b.high >= b.low > 0 for b in bars)
    assert all(b.ts_close == b.ts_open + timedelta(days=1) for b in bars)
    assert bars == sorted(bars, key=lambda b: b.ts_open)
