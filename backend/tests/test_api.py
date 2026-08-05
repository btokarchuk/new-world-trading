"""The API is the same witness over HTTP: right shapes, read-only by
construction, and graceful (not dead, not lying) when the db is gone."""

import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from nwt_backend.api import create_app
from nwt_contracts import Side
from nwt_engine.sleeves import LedgerEntry
from nwt_risk.supervision import SupervisionStore

from fixture_db import NOW, build_fixture


@pytest.fixture
def client(fixture_db):
    db, config = fixture_db
    app = create_app(db_path=db, paper_config=config, now_fn=lambda: NOW)
    with TestClient(app) as c:
        yield c, db


def _dec(value) -> Decimal:
    return Decimal(str(value))


# -- health ------------------------------------------------------------------


def test_health_ok(client):
    c, _db = client
    body = c.get("/health").json()
    assert body["status"] == "ok"
    assert body["phase"] == "poll"
    assert body["overdue_s"] < 0  # promise still in the future


def test_health_overdue(client):
    c, db = client
    # The engine breaks its promise: last beat's next_due is 30 minutes ago.
    SupervisionStore(db).beat(
        NOW - timedelta(hours=1), NOW - timedelta(minutes=30), "cycle", "wedged"
    )
    body = c.get("/health").json()
    assert body["status"] == "overdue"
    assert 1799 < body["overdue_s"] < 1801


def test_health_without_database_answers_instead_of_erroring(tmp_path):
    app = create_app(db_path=tmp_path / "gone.db", paper_config=tmp_path / "paper.yaml")
    with TestClient(app) as c:
        response = c.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "no_database"


# -- data endpoints ----------------------------------------------------------


def test_status_shape(client):
    c, _db = client
    body = c.get("/status").json()
    assert body["state"]["state"] == "HALTED"
    assert body["state"]["mode"] == "paper"
    assert body["state"]["armed"] is False
    [latch] = body["latches"]
    assert latch["breaker"] == "startup" and latch["acked"] is False
    assert latch["age_s"] == pytest.approx(7200)
    [alert] = body["alerts"]
    assert alert["severity"] == "CRITICAL" and alert["acked_at"] is None
    assert body["pending_commands"] == 0


def test_positions_folded_with_marks(client):
    c, _db = client
    body = c.get("/positions").json()
    assert body["gaps"] == []
    sleeves = {s["sleeve_id"]: s for s in body["sleeves"]}
    control = sleeves["control"]
    assert _dec(control["cash"]) == Decimal("1730.75")
    [spy] = control["positions"]
    assert spy["symbol"] == "SPY"
    assert _dec(spy["qty"]) == 1
    assert _dec(spy["avg_cost"]) == Decimal("769.25")
    assert _dec(spy["mark"]) == Decimal("771.00")
    assert _dec(spy["open_pnl"]) == Decimal("1.75")
    assert _dec(control["equity"]) == Decimal("2501.75")
    [eem] = sleeves["momentum"]["positions"]
    assert _dec(eem["open_pnl"]) == Decimal("-28.20")


def test_positions_suppress_broken_fold_instead_of_500(tmp_path):
    # A journal row the fold cannot apply must neither crash the endpoint nor
    # let the broken sleeve's numbers ride with full authority.
    db, config = build_fixture(tmp_path)
    bad = LedgerEntry(
        kind="fill", ts=NOW - timedelta(hours=1), symbol="EEM", side=Side.SELL,
        qty=Decimal("100"), price=Decimal("64.00"), fees=Decimal("0"),
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO ledger_entries (sleeve_id, entry_json, ts) VALUES (?,?,?)",
            ("momentum", bad.model_dump_json(), bad.ts.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    app = create_app(db_path=db, paper_config=config, now_fn=lambda: NOW)
    with TestClient(app) as c:
        response = c.get("/positions")
    assert response.status_code == 200
    body = response.json()
    momentum = {s["sleeve_id"]: s for s in body["sleeves"]}["momentum"]
    assert momentum["fold_complete"] is False
    assert momentum["equity"] is None
    assert all(p["mark"] is None for p in momentum["positions"])
    assert any("fold violated an invariant" in gap for gap in body["gaps"])


def test_positions_gap_when_mark_missing(tmp_path):
    db, config = build_fixture(tmp_path, symbols_with_marks=("SPY",))
    app = create_app(db_path=db, paper_config=config, now_fn=lambda: NOW)
    with TestClient(app) as c:
        body = c.get("/positions").json()
    assert any("Cannot explain open P&L for EEM" in gap for gap in body["gaps"])
    [eem] = {s["sleeve_id"]: s for s in body["sleeves"]}["momentum"]["positions"]
    assert eem["mark"] is None and eem["open_pnl"] is None


def test_fills_days_window(client):
    c, _db = client
    today_only = c.get("/fills", params={"days": 1}).json()
    assert [f["symbol"] for f in today_only] == ["EEM"]
    both = c.get("/fills", params={"days": 2}).json()
    assert [f["symbol"] for f in both] == ["SPY", "EEM"]
    assert _dec(both[1]["qty"]) == 15
    assert c.get("/fills", params={"days": 0}).status_code == 422


def test_equity_series(client):
    c, _db = client
    body = c.get("/equity").json()
    assert [p["day"] for p in body] == ["2026-08-04", "2026-08-05"]
    assert _dec(body[-1]["equity"]) == Decimal("10001.5")


def test_report_today_is_markdown(client):
    c, _db = client
    response = c.get("/report/today")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "# Daily report — 2026-08-05" in response.text


def test_missing_db_is_503_on_data_endpoints(tmp_path):
    config = tmp_path / "paper.yaml"
    config.write_text("sleeves: []\n")
    app = create_app(db_path=tmp_path / "gone.db", paper_config=config)
    with TestClient(app) as c:
        for path in ("/status", "/positions", "/fills", "/equity", "/report/today"):
            response = c.get(path)
            assert response.status_code == 503, path
            assert "not found" in response.json()["detail"]


# -- structural: this API cannot mutate --------------------------------------


def test_every_route_is_read_only(fixture_db):
    db, config = fixture_db
    app = create_app(db_path=db, paper_config=config)
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        assert methods <= {"GET", "HEAD"}, f"{route.path} allows {methods}"
