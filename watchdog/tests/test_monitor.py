import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from nwt_watchdog.alerts import WatchdogAlerts
from nwt_watchdog.broker import AlpacaReadOnly
from nwt_watchdog.config import WatchdogConfig
from nwt_watchdog.monitor import Watchdog

NOW = datetime(2026, 8, 3, 14, 30, tzinfo=UTC)
HEALTHCHECK = "https://hc.example/deadbeef"
SRC = Path(__file__).resolve().parents[1] / "src" / "nwt_watchdog"

# Copied from nwt_risk.supervision rather than imported: importing the engine's
# code into the watchdog's tests would hide exactly the coupling this package
# exists to avoid. If this drifts from that file, the watchdog is broken.
_RISK_SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeats (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    seq      INTEGER NOT NULL,
    ts       TEXT NOT NULL,
    next_due TEXT NOT NULL,
    phase    TEXT NOT NULL,
    detail   TEXT NOT NULL
);
"""


def write_heartbeat(db_path: Path, next_due: datetime, phase: str = "cycle") -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_RISK_SCHEMA)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO heartbeats (id, seq, ts, next_due, phase, detail)"
            " VALUES (1, ?, ?, ?, ?, ?)",
            (3, (next_due - timedelta(seconds=60)).isoformat(), next_due.isoformat(), phase, ""),
        )
    conn.close()


def risk_tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    return {n for n in names if not n.startswith("sqlite_")}


def halt_rows(db_path: Path) -> list[dict]:
    if not db_path.exists() or "control_commands" not in risk_tables(db_path):
        return []
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT command_id, ts, command, issuer, reason, consumed FROM control_commands"
    ).fetchall()
    conn.close()
    return [
        {
            "command_id": r[0],
            "ts": r[1],
            "command": r[2],
            "issuer": r[3],
            "reason": r[4],
            "consumed": r[5],
        }
        for r in rows
    ]


class FakeBroker:
    def __init__(
        self,
        *,
        account: dict | None = None,
        positions: list[dict] | None = None,
        open_orders: list[dict] | None = None,
        orders: list[dict] | None = None,
        raises: Exception | None = None,
        cancel_raises: Exception | None = None,
    ) -> None:
        self._account = account or {
            "equity": Decimal("10000"),
            "last_equity": Decimal("10000"),
            "cash": Decimal("1000"),
        }
        self._positions = positions or []
        self._open_orders = open_orders or []
        self._orders = orders or []
        self._raises = raises
        self._cancel_raises = cancel_raises
        self.cancel_calls = 0
        self.since: list[datetime] = []

    def _guard(self) -> None:
        if self._raises is not None:
            raise self._raises

    def account(self) -> dict:
        self._guard()
        return dict(self._account)

    def positions(self) -> list[dict]:
        self._guard()
        return list(self._positions)

    def open_orders(self) -> list[dict]:
        self._guard()
        return list(self._open_orders)

    def orders_since(self, since: datetime) -> list[dict]:
        self._guard()
        self.since.append(since)
        return list(self._orders)

    def cancel_all(self) -> dict:
        self.cancel_calls += 1
        if self._cancel_raises is not None:
            raise self._cancel_raises
        return {"http_status": 207, "cancelled": ["ord-1"], "failed": []}


def make_alerts(tmp_path, *, webhook=None, status=200, boom=False):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if boom:
            raise httpx.ConnectError("network down")
        return httpx.Response(status)

    alerts = WatchdogAlerts(
        tmp_path / "state" / "watchdog.db",
        lambda: NOW,
        webhook_url=webhook,
        healthcheck_url=HEALTHCHECK,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return alerts, calls


class StopAfter:
    """sleep_fn that ends run_forever after N cycles; `watchdog` is wired in
    once the object it has to stop exists."""

    def __init__(self, cycles: int) -> None:
        self.cycles = cycles
        self.slept: list[float] = []
        self.watchdog: Watchdog | None = None

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        if len(self.slept) >= self.cycles and self.watchdog is not None:
            self.watchdog.stop()


def make_watchdog(
    tmp_path, broker, *, risk_db=None, dry_run=False, sleep_fn=None, config_kw=None, **alert_kw
):
    alerts, calls = make_alerts(tmp_path, **alert_kw)
    config = WatchdogConfig(
        risk_db=risk_db if risk_db is not None else tmp_path / "data" / "risk.db",
        state_db=tmp_path / "state" / "watchdog.db",
        healthcheck_url=HEALTHCHECK,
        dry_run=dry_run,
        # Coverage check off by default HERE ONLY (overridable): these fixtures
        # predate protective stops and hold naked positions by construction.
        # The detector has its own tests below with it enabled.
        **{"protection_check": False, **(config_kw or {})},
    )
    watchdog = Watchdog(
        config, broker, lambda: NOW, alerts, **({"sleep_fn": sleep_fn} if sleep_fn else {})
    )
    if isinstance(sleep_fn, StopAfter):
        sleep_fn.watchdog = watchdog
    return watchdog, alerts, calls, config


# -- the core sequence ------------------------------------------------------


def test_overdue_heartbeat_cancels_halts_and_pages(tmp_path):
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW - timedelta(minutes=10))
    broker = FakeBroker()
    watchdog, alerts, calls, _ = make_watchdog(tmp_path, broker, risk_db=risk_db)

    breaches = watchdog.check()
    assert [(b.name, b.severity) for b in breaches] == [("heartbeat_overdue", "CRITICAL")]

    watchdog.act(breaches)

    assert broker.cancel_calls == 1
    (halt,) = halt_rows(risk_db)
    assert halt["command"] == "HALT"
    assert halt["issuer"] == "watchdog"
    assert halt["consumed"] == 0
    assert "heartbeat_overdue" in halt["reason"]
    assert halt["ts"] == NOW.isoformat()

    assert len(calls) == 1
    method, url = calls[0]
    assert method == "GET"
    assert url.startswith(f"{HEALTHCHECK}/fail")

    critical = [a for a in alerts.alerts() if a["severity"] == "CRITICAL"]
    assert len(critical) == 1
    assert critical[0]["category"] == "watchdog_breach"
    assert critical[0]["payload"]["halt_command_id"] == halt["command_id"]
    assert critical[0]["payload"]["cancel"]["cancelled"] == ["ord-1"]
    assert critical[0]["payload"]["dry_run"] is False

    # The HALT row is the ONLY thing the watchdog writes to the engine's db.
    assert risk_tables(risk_db) == {"heartbeats", "control_commands"}


def test_healthy_snapshot_takes_no_action_and_pings_ok(tmp_path):
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW + timedelta(seconds=60))
    broker = FakeBroker(
        positions=[{"symbol": "SPY", "qty": "4", "market_value": "3000"}],
        open_orders=[{"id": "o1", "symbol": "SPY", "created_at": NOW.isoformat()}],
        orders=[{"id": "o1", "symbol": "SPY", "created_at": NOW.isoformat()}],
    )
    watchdog, alerts, calls, _ = make_watchdog(tmp_path, broker, risk_db=risk_db)

    breaches = watchdog.check()
    assert breaches == []

    watchdog.act(breaches)

    assert broker.cancel_calls == 0
    assert halt_rows(risk_db) == []
    assert risk_tables(risk_db) == {"heartbeats"}
    assert calls == [("GET", HEALTHCHECK)]
    assert alerts.alerts() == []
    assert broker.since == [NOW - timedelta(minutes=10)]


def test_missing_risk_db_is_a_breach_not_a_crash(tmp_path):
    risk_db = tmp_path / "nowhere" / "risk.db"
    broker = FakeBroker()
    watchdog, alerts, calls, _ = make_watchdog(tmp_path, broker, risk_db=risk_db)

    assert watchdog.read_heartbeat() is None
    breaches = watchdog.check()
    assert [b.observed for b in breaches] == ["no heartbeat channel"]

    watchdog.act(breaches)
    assert broker.cancel_calls == 1
    (halt,) = halt_rows(risk_db)  # the db is created just to file the HALT
    assert halt["command"] == "HALT"
    assert calls[0][1].startswith(f"{HEALTHCHECK}/fail")


def test_risk_db_without_a_heartbeat_table_is_a_breach_not_a_crash(tmp_path):
    risk_db = tmp_path / "data" / "risk.db"
    risk_db.parent.mkdir(parents=True)
    sqlite3.connect(str(risk_db)).close()
    watchdog, _, _, _ = make_watchdog(tmp_path, FakeBroker(), risk_db=risk_db)
    assert watchdog.read_heartbeat() is None


def test_dry_run_suppresses_the_cancel_but_still_halts_and_pages(tmp_path):
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW - timedelta(minutes=10))
    broker = FakeBroker()
    watchdog, alerts, calls, _ = make_watchdog(tmp_path, broker, risk_db=risk_db, dry_run=True)

    watchdog.act(watchdog.check())

    assert broker.cancel_calls == 0
    (critical,) = [a for a in alerts.alerts() if a["severity"] == "CRITICAL"]
    assert critical["payload"]["cancel"] == {"dry_run": True, "would_cancel_all": True}
    assert critical["payload"]["dry_run"] is True
    assert len(halt_rows(risk_db)) == 1
    assert calls[0][1].startswith(f"{HEALTHCHECK}/fail")


def test_cancel_failure_still_halts_and_pages(tmp_path):
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW - timedelta(minutes=10))
    broker = FakeBroker(cancel_raises=httpx.ConnectError("broker unreachable"))
    watchdog, alerts, calls, _ = make_watchdog(tmp_path, broker, risk_db=risk_db)

    watchdog.act(watchdog.check())  # must not raise

    assert broker.cancel_calls == 1
    (critical,) = [a for a in alerts.alerts() if a["severity"] == "CRITICAL"]
    assert "ConnectError" in critical["payload"]["cancel"]["error"]
    assert len(halt_rows(risk_db)) == 1
    assert calls[0][1].startswith(f"{HEALTHCHECK}/fail")


def test_multiple_breaches_all_reach_the_halt_reason(tmp_path):
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW + timedelta(seconds=60))
    broker = FakeBroker(
        account={"equity": "8000", "last_equity": "10000", "cash": "0"},
        positions=[{"symbol": "SPY", "qty": "1", "market_value": "9600"}],
    )
    watchdog, _, _, _ = make_watchdog(tmp_path, broker, risk_db=risk_db)

    names = [b.name for b in watchdog.check()]
    assert names == ["gross_exposure", "daily_pnl_floor", "equity_floor"]

    watchdog.act(watchdog.check())
    (halt,) = halt_rows(risk_db)
    for name in names:
        assert name in halt["reason"]


def test_warn_only_breach_alerts_without_cancelling(tmp_path):
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW + timedelta(seconds=60))
    broker = FakeBroker(account={"equity": "10000", "last_equity": "0", "cash": "0"})
    watchdog, alerts, calls, _ = make_watchdog(tmp_path, broker, risk_db=risk_db)

    breaches = watchdog.check()
    assert [(b.name, b.severity) for b in breaches] == [("daily_pnl_floor", "WARN")]

    watchdog.act(breaches)
    assert broker.cancel_calls == 0
    assert halt_rows(risk_db) == []
    assert [a["severity"] for a in alerts.alerts()] == ["WARN"]
    assert calls == [("GET", HEALTHCHECK)]  # alive and not blind to an emergency


# -- the loop ---------------------------------------------------------------


def test_broker_failure_alerts_and_the_loop_survives(tmp_path):
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW + timedelta(seconds=60))
    broker = FakeBroker(raises=httpx.ConnectError("broker down"))
    stopper = StopAfter(3)
    watchdog, alerts, calls, config = make_watchdog(
        tmp_path, broker, risk_db=risk_db, sleep_fn=stopper
    )

    watchdog.run_forever()  # must return, not raise

    assert stopper.slept == [config.poll_interval_s] * 3
    errors = [a for a in alerts.alerts() if a["category"] == "watchdog_error"]
    assert len(errors) == 3
    assert "ConnectError" in errors[0]["message"]
    assert "Traceback" in errors[0]["payload"]["traceback"]
    assert len(calls) == 3
    assert all(url.startswith(f"{HEALTHCHECK}/fail") for _, url in calls)
    assert halt_rows(risk_db) == []  # a blind watchdog reports; it does not guess


def test_run_forever_stops_cleanly(tmp_path):
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW + timedelta(seconds=60))
    stopper = StopAfter(1)
    watchdog, _, calls, _ = make_watchdog(
        tmp_path, FakeBroker(), risk_db=risk_db, sleep_fn=stopper
    )

    watchdog.run_forever()
    assert stopper.slept == [60]
    assert calls == [("GET", HEALTHCHECK)]


# -- alert isolation --------------------------------------------------------


def test_webhook_failure_is_recorded_never_raised(tmp_path):
    alerts, calls = make_alerts(tmp_path, webhook="https://push.example/hook", boom=True)
    alert_id = alerts.raise_alert("CRITICAL", "watchdog_breach", "boom", {"k": "v"})

    assert alert_id == 1
    assert [c[0] for c in calls] == ["POST"]
    (delivery,) = alerts.deliveries()
    assert delivery["channel"] == "webhook"
    assert delivery["ok"] is False
    assert "ConnectError" in delivery["detail"]
    assert alerts.alerts()[0]["message"] == "boom"  # the record survives the failed send


def test_healthcheck_failure_is_recorded_never_raised(tmp_path):
    alerts, _ = make_alerts(tmp_path, boom=True)
    alerts.ping_ok()
    (delivery,) = alerts.deliveries()
    assert delivery["channel"] == "healthcheck_ok"
    assert delivery["ok"] is False


def test_watchdog_alerts_never_touch_the_engine_db(tmp_path):
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW - timedelta(minutes=10))
    watchdog, alerts, _, config = make_watchdog(tmp_path, FakeBroker(), risk_db=risk_db)
    watchdog.act(watchdog.check())

    assert "watchdog_alerts" not in risk_tables(risk_db)
    assert "deliveries" not in risk_tables(risk_db)
    assert config.state_db != config.risk_db
    assert len(alerts.alerts()) == 1


# -- broker shapes ----------------------------------------------------------


def _broker(handler) -> AlpacaReadOnly:
    return AlpacaReadOnly(
        "https://paper-api.example",
        "key",
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_cancel_all_parses_the_207_multi_status_body():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(
            207,
            json=[
                {"id": "a", "status": 200},
                {"id": "b", "status": 500, "body": {"message": "already filled"}},
            ],
        )

    result = _broker(handler).cancel_all()
    assert seen == [("DELETE", "/v2/orders")]
    assert result["cancelled"] == ["a"]
    assert result["failed"] == [{"id": "b", "status": 500, "body": {"message": "already filled"}}]
    assert result["http_status"] == 207


def test_read_endpoints_parse_decimals_and_timestamps():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/account":
            return httpx.Response(
                200,
                json={
                    "equity": "9950.25",
                    "last_equity": "10000",
                    "cash": "12.5",
                    "status": "ACTIVE",
                },
            )
        if request.url.path == "/v2/positions":
            return httpx.Response(
                200, json=[{"symbol": "SPY", "qty": "4", "market_value": "-3000.10"}]
            )
        return httpx.Response(
            200,
            json=[{"id": "o1", "symbol": "SPY", "created_at": "2026-08-03T14:29:59.123456789Z"}],
        )

    broker = _broker(handler)
    assert broker.account() == {
        "equity": Decimal("9950.25"),
        "last_equity": Decimal("10000"),
        "cash": Decimal("12.5"),
        "status": "ACTIVE",
    }
    assert broker.positions() == [
        {
            "symbol": "SPY",
            "asset_class": "",
            "qty": Decimal("4"),
            "market_value": Decimal("-3000.10"),
        }
    ]
    order = broker.open_orders()[0]
    assert order["created_at"] == datetime(2026, 8, 3, 14, 29, 59, 123456, tzinfo=UTC)
    assert broker.orders_since(NOW)[0]["id"] == "o1"


def test_broker_raises_on_http_error():
    with pytest.raises(httpx.HTTPStatusError):
        _broker(lambda request: httpx.Response(403, json={"message": "forbidden"})).account()


# -- the isolation invariants -----------------------------------------------

_MUTATING = re.compile(r"""["'](POST|PUT|PATCH|DELETE)["']|\.(post|put|patch|delete)\s*\(""")
_FORBIDDEN_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(nwt_engine|nwt_risk|nwt_contracts|pandas)\b", re.MULTILINE
)


def _source_lines():
    for path in sorted(SRC.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            yield path.name, lineno, line.strip()


def test_cancel_all_is_the_only_mutating_call_in_the_package():
    hits = [(name, lineno, line) for name, lineno, line in _source_lines() if _MUTATING.search(line)]

    broker_hits = [h for h in hits if h[0] == "broker.py"]
    assert len(broker_hits) == 1, broker_hits
    assert broker_hits[0][2] == 'response = self._send("DELETE", "/v2/orders")'

    verbs = re.findall(r'self\._send\(\s*"([A-Z]+)"', (SRC / "broker.py").read_text())
    assert sorted(verbs) == ["DELETE", "GET"]

    # The only other mutating verb in the package is the operator webhook POST,
    # which cannot reach the broker: alerts.py never learns an Alpaca URL.
    others = [h for h in hits if h[0] != "broker.py"]
    assert {h[0] for h in others} == {"alerts.py"}
    assert all("self._webhook" in h[2] for h in others), others
    assert "alpaca" not in (SRC / "alerts.py").read_text(encoding="utf-8").lower()


def test_package_never_imports_the_system_it_supervises():
    offenders = [
        path.name
        for path in sorted(SRC.rglob("*.py"))
        if _FORBIDDEN_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_standing_breach_does_not_re_page_or_re_halt_every_poll(tmp_path):
    """Regression (2026-08-03): one overnight condition produced 315 identical
    HALT rows and 315 CRITICALs. Cancels must keep firing while the breach
    stands; command rows and pages must not."""
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW - timedelta(minutes=10))
    broker = FakeBroker()
    watchdog, alerts, calls, _ = make_watchdog(tmp_path, broker, risk_db=risk_db)

    for _ in range(6):
        watchdog.act(watchdog.check())

    assert broker.cancel_calls == 6, "cancels keep firing while the breach stands"
    assert len(halt_rows(risk_db)) == 1, "one unconsumed HALT is enough"
    pages = [a for a in alerts.alerts() if a["category"] == "watchdog_breach"]
    assert len(pages) == 1, f"repeat pages must back off, got {len(pages)}"
    # The /fail ping still goes out every poll: healthchecks.io escalation is
    # driven by continued silence, not by how loudly we page.
    assert len([c for c in calls if "/fail" in c[1]]) == 6


def test_host_suspend_is_not_treated_as_a_wedged_engine(tmp_path):
    """Regression (2026-08-04): a laptop sleeping in short bursts made the beat
    4-11 minutes late against a 180s grace. The watchdog HALTed five times and
    cost a full trading day. A suspend freezes supervisor and engine together,
    so if OUR loop lost as much time as the beat is late, that explains it."""
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW - timedelta(minutes=10))  # 600s late
    broker = FakeBroker()
    watchdog, alerts, calls, _ = make_watchdog(tmp_path, broker, risk_db=risk_db)

    # Sparse attendance: our loop ran a handful of times over the last hour
    # (chained brief wakes) instead of the ~60 polls a healthy host would run.
    for minutes in (60, 45, 30, 15):
        watchdog._loop_history.append(NOW - timedelta(minutes=minutes))
    watchdog.act(watchdog.check())

    assert broker.cancel_calls == 0, "a suspended host must not trigger a cancel"
    assert halt_rows(risk_db) == [], "a suspended host must not HALT the engine"
    warns = [a for a in alerts.alerts() if a["category"] == "watchdog_suspend"]
    assert len(warns) == 1
    assert warns[0]["severity"] == "WARN"
    assert "host suspended" in warns[0]["message"]


def test_wedged_engine_still_halts_when_the_watchdog_was_running(tmp_path):
    """The converse, and the one that matters: if our loop kept its cadence and
    the engine still missed its promise, that IS a hang. Suspend detection must
    not become a blanket excuse."""
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW - timedelta(minutes=10))
    broker = FakeBroker()
    watchdog, alerts, calls, _ = make_watchdog(tmp_path, broker, risk_db=risk_db)

    # Full attendance: our loop ran every interval through the late window.
    interval = watchdog._config.poll_interval_s
    for i in range(1, 15):
        watchdog._loop_history.append(NOW - timedelta(seconds=i * interval))
    watchdog.act(watchdog.check())

    assert broker.cancel_calls == 1
    assert len(halt_rows(risk_db)) == 1
    assert [a for a in alerts.alerts() if a["category"] == "watchdog_breach"]


# -- unprotected-position coverage (docs/design/protective-stops.md §4.6) ----


def _coverage(positions, orders, **config_kw):
    from nwt_watchdog.config import WatchdogConfig
    from nwt_watchdog.invariants import unprotected_positions

    config = WatchdogConfig(
        risk_db=Path("unused"), state_db=Path("unused"), **config_kw
    )
    return unprotected_positions(positions, orders, config)


def test_naked_equity_position_warns():
    breaches = _coverage(
        [{"symbol": "IWM", "asset_class": "us_equity", "qty": Decimal("3")}], []
    )
    assert [(b.name, b.severity) for b in breaches] == [("unprotected_position", "WARN")]
    assert "3 unprotected of 3" in breaches[0].observed


def test_resting_sell_stops_cover_the_position():
    breaches = _coverage(
        [{"symbol": "IWM", "asset_class": "us_equity", "qty": Decimal("3")}],
        [
            {"symbol": "IWM", "side": "sell", "type": "stop", "qty": "2"},
            {"symbol": "IWM", "side": "sell", "type": "stop", "qty": "1"},
        ],
    )
    assert breaches == []


def test_sell_limits_and_buy_stops_do_not_count_as_protection():
    breaches = _coverage(
        [{"symbol": "IWM", "asset_class": "us_equity", "qty": Decimal("2")}],
        [
            {"symbol": "IWM", "side": "sell", "type": "limit", "qty": "2"},
            {"symbol": "IWM", "side": "buy", "type": "stop", "qty": "2"},
        ],
    )
    assert len(breaches) == 1 and breaches[0].name == "unprotected_position"


def test_allowance_excuses_the_control_lot():
    # Owner decision (design §8 row 1): the benchmark rides crashes unprotected.
    breaches = _coverage(
        [{"symbol": "SPY", "asset_class": "us_equity", "qty": Decimal("1")}],
        [],
        protection_allowances={"SPY": "1"},
    )
    assert breaches == []


def test_stale_allowance_fires_its_companion_breach():
    # An allowance larger than the position it excuses means the config is
    # stale and the symbol's coverage check is quietly toothless.
    breaches = _coverage(
        [{"symbol": "SPY", "asset_class": "us_equity", "qty": Decimal("1")}],
        [],
        protection_allowances={"SPY": "3"},
    )
    assert [b.name for b in breaches] == ["stale_protection_allowance"]


def test_crypto_positions_are_exempt():
    breaches = _coverage(
        [{"symbol": "BTCUSD", "asset_class": "crypto", "qty": Decimal("0.0063")}], []
    )
    assert breaches == []


def test_promotion_flag_escalates_to_critical():
    breaches = _coverage(
        [{"symbol": "IWM", "asset_class": "us_equity", "qty": Decimal("3")}],
        [],
        protection_critical=True,
    )
    assert breaches[0].severity == "CRITICAL"


def test_two_standing_warns_back_off_independently(tmp_path):
    """Alternating breaches must not reset each other's paging clocks."""
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW + timedelta(seconds=60))  # healthy heart
    broker = FakeBroker(
        positions=[
            {"symbol": "EEM", "asset_class": "us_equity", "qty": "15", "market_value": "990"},
            {"symbol": "IWM", "asset_class": "us_equity", "qty": "3", "market_value": "900"},
        ]
    )
    watchdog, alerts, calls, _ = make_watchdog(
        tmp_path, broker, config_kw={"protection_check": True}
    )
    for _ in range(5):
        watchdog.act(watchdog.check())
    warns = [a for a in alerts.alerts() if a["category"] == "unprotected_position"]
    # one page per symbol, not one per symbol per poll
    assert len(warns) == 2, [w["message"] for w in warns]


def test_chained_naps_are_still_recognized_as_suspension(tmp_path):
    """Regression (2026-08-05 06:12): macOS power-nap wakes let the watchdog
    re-baseline on each brief wake while the engine's 30-minute sleep chunk
    never completed. Single-gap comparison saw a small recent gap against a
    2122s-late beat and cancelled the resting stops. Attendance over the whole
    late window must recognize the pattern: few polls ran, host was frozen."""
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW - timedelta(seconds=2122))
    broker = FakeBroker()
    watchdog, alerts, calls, _ = make_watchdog(tmp_path, broker, risk_db=risk_db)

    # 2122s window at 60s polls should hold ~35 loops; only 3 brief wakes ran,
    # the LAST one recently (the re-baseline that used to defeat detection).
    for seconds in (2000, 1000, 30):
        watchdog._loop_history.append(NOW - timedelta(seconds=seconds))

    watchdog.act(watchdog.check())

    assert broker.cancel_calls == 0, "chained naps must not cancel resting stops"
    assert halt_rows(risk_db) == []
    assert [a["category"] for a in alerts.alerts() if a["severity"] == "WARN"] == [
        "watchdog_suspend"
    ]


def test_restarted_watchdog_with_no_history_still_acts(tmp_path):
    """An empty loop history must never excuse a late engine: a freshly
    restarted supervisor errs toward acting."""
    risk_db = tmp_path / "data" / "risk.db"
    write_heartbeat(risk_db, NOW - timedelta(minutes=30))
    broker = FakeBroker()
    watchdog, alerts, calls, _ = make_watchdog(tmp_path, broker, risk_db=risk_db)

    watchdog.act(watchdog.check())

    assert broker.cancel_calls == 1
    assert len(halt_rows(risk_db)) == 1
