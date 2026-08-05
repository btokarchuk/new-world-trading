"""The report is a witness: correct against a real-schema db, honest when the
evidence runs out, and physically unable to write."""

import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from nwt_backend.cli import app
from nwt_backend.observe import RiskDbUnavailable, open_risk_db
from nwt_backend.report import build_snapshot, generate, render_markdown
from nwt_engine.sleeves import LedgerEntry
from nwt_contracts import Side

from fixture_db import NOW, build_fixture


# -- correct rendering against the real schemas ------------------------------


def test_report_renders_fixture(fixture_db, tmp_path):
    db, config = fixture_db
    path, md = generate(db, config, tmp_path / "reports", NOW)

    assert path == tmp_path / "reports" / "2026-08-05.md"
    assert path.read_text() == md
    assert "# Daily report — 2026-08-05" in md

    # state + latch with age, alert with age
    assert "**HALTED**" in md
    assert "startup" in md and "age 2h00m" in md
    assert "CRITICAL scheduler: reconcile mismatch — HALTED (age 1h00m)" in md

    # heartbeat promise held
    assert "on time" in md and "OVERDUE" not in md

    # activity: 2 reconcile passes today (4 audit rows — the engine writes each
    # pass twice), 1 order, rejections from the governor's real verdict (the
    # state gate and the structural gate both record STATE_NOT_ACTIVE)
    assert "Reconcile passes: 2" in md
    assert "reconcile 4" in md  # raw row count stays visible and honest
    assert "Orders submitted: 1" in md
    assert "ORDER_NOTIONAL_CAP ×1" in md and "STATE_NOT_ACTIVE ×2" in md

    # fills grouped by day and sleeve
    today, yesterday = md.split("### Yesterday")
    assert "momentum" in today and "EEM" in today and "65.88" in today
    assert "control" in yesterday and "SPY" in yesterday and "769.25" in yesterday

    # positions folded through the real ledger, P&L from stored closes
    assert "| SPY | 1 | 769.25 | 771.00 | 771.00 | +1.75 |" in md
    assert "| EEM | 15 | 65.88 | 64.00 | 960.00 | -28.20 |" in md
    assert "cash 1730.75" in md   # 2500 - 769.25
    assert "cash 1011.80" in md   # 2000 - 15*65.88

    # equity trajectory
    assert "| 2026-08-04 | 10000 |" in md
    assert "| 2026-08-05 | 10001.5 |" in md
    assert "+1.5" in md

    # a fully-evidenced day narrates without disclaimers
    assert "cannot explain" not in md.lower()


def test_snapshot_numbers_trace_to_rows(fixture_db):
    db, config = fixture_db
    snap = build_snapshot(db, config, NOW)
    control = next(s for s in snap.sleeves if s.sleeve_id == "control")
    assert control.equity == Decimal("2501.75")  # 1730.75 cash + 771.00 mark
    assert snap.activity.rejections_by_reason == {
        "STATE_NOT_ACTIVE": 2,  # state_gate check + the structural gate
        "ORDER_NOTIONAL_CAP": 1,
    }
    assert len(snap.fills_today) == 1 and len(snap.fills_yesterday) == 1


def test_reconcile_audit_pairs_count_as_single_passes(fixture_db):
    # Every engine reconcile pass writes TWO audit rows carrying the identical
    # inner report (ReconcileEngine.reconcile + reconcile_and_arm). The
    # narrative number is passes, not rows.
    db, config = fixture_db
    snap = build_snapshot(db, config, NOW)
    assert snap.activity.audit_counts["reconcile"] == 4  # two passes today
    assert snap.activity.reconcile_events == 2


# -- read-only enforcement ---------------------------------------------------


def test_connection_refuses_writes(fixture_db):
    db, _config = fixture_db
    conn = open_risk_db(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO audit (ts, kind, payload) VALUES ('x','x','{}')")
    finally:
        conn.close()


def test_missing_db_fails_gracefully(tmp_path):
    with pytest.raises(RiskDbUnavailable, match="not found"):
        open_risk_db(tmp_path / "nope.db")

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paper.yaml").write_text("sleeves: []\n")
    result = CliRunner().invoke(
        app,
        [
            "report",
            "--db", str(tmp_path / "nope.db"),
            "--config", str(tmp_path / "config" / "paper.yaml"),
            "--out-dir", str(tmp_path / "reports"),
        ],
    )
    assert result.exit_code == 1
    assert "cannot report" in result.output
    assert not (tmp_path / "reports").exists()


# -- empty db: say so, don't crash, don't invent -----------------------------


def test_empty_db_reports_emptiness(tmp_path):
    db = tmp_path / "risk.db"
    db.touch()  # zero-byte file: a valid, completely empty sqlite db
    config = tmp_path / "paper.yaml"
    config.write_text("sleeves: []\n")

    _path, md = generate(db, config, tmp_path / "reports", NOW)
    assert "no evidence of any activity" in md.lower()
    assert "deliberately invents none" in md


def test_info_alerts_are_counted_but_not_itemized(fixture_db):
    # Every CLI run writes an un-acked INFO alert; the report must keep the
    # count honest without letting that noise bury WARN-and-above rows.
    db, config = fixture_db
    from nwt_risk.alerts import AlertOutbox

    outbox = AlertOutbox(db, lambda: NOW - timedelta(minutes=5))
    for _ in range(3):
        outbox.raise_alert("INFO", "command", "nwt-risk status", {})
    snap = build_snapshot(db, config, NOW)
    md = render_markdown(snap)
    assert "Un-acked alerts: 4" in md
    assert "plus 3 un-acked INFO alert(s)" in md
    assert "nwt-risk status" not in md
    assert "CRITICAL scheduler" in md  # the itemized one survives


# -- the anti-confabulation law ----------------------------------------------


def test_position_without_stored_close_is_declared_not_invented(tmp_path):
    db, config = build_fixture(tmp_path, symbols_with_marks=("SPY",))
    _path, md = generate(db, config, tmp_path / "reports", NOW)
    assert "Cannot explain open P&L for EEM from evidence" in md
    # the unpriceable position renders with em-dashes, never a made-up mark
    assert "| EEM | 15 | 65.88 | — | — | — |" in md
    # and the priced position still gets its evidenced P&L
    assert "+1.75" in md


def test_ledger_fills_without_audit_trail_are_declared(tmp_path):
    db, config = build_fixture(tmp_path, with_fill_audit=False)
    _path, md = generate(db, config, tmp_path / "reports", NOW)
    assert (
        "Cannot explain 1 ledger fill(s) applied today from evidence:"
        " no fill or cross audit row records their arrival" in md
    )


def test_entries_for_unknown_sleeve_are_declared(tmp_path):
    ghost_entry = LedgerEntry(
        kind="fill",
        ts=NOW - timedelta(hours=1),
        symbol="QQQ",
        side=Side.BUY,
        qty=Decimal("1"),
        price=Decimal("100"),
        fees=Decimal("0"),
    )
    db, config = build_fixture(tmp_path, extra_sleeve_entries=(("ghost", ghost_entry),))
    _path, md = generate(db, config, tmp_path / "reports", NOW)
    assert "Cannot explain 1 ledger entry for sleeve 'ghost' from evidence" in md


def test_partially_unexplained_fills_are_declared(tmp_path):
    # Two ledger fills today, but the audit trail's one fill row (no net plan)
    # accounts for at most one of them: the shortfall is a gap, not silence.
    extra = LedgerEntry(
        kind="fill",
        ts=NOW - timedelta(hours=1),
        symbol="EEM",
        side=Side.BUY,
        qty=Decimal("1"),
        price=Decimal("64.00"),
        fees=Decimal("0"),
    )
    db, config = build_fixture(tmp_path, extra_sleeve_entries=(("momentum", extra),))
    _path, md = generate(db, config, tmp_path / "reports", NOW)
    assert (
        "Cannot explain 1 of 2 ledger fill(s) applied today from evidence:"
        " the fill and cross audit rows account for at most 1" in md
    )


def _insert_raw_entry(db, sleeve_id: str, entry: LedgerEntry) -> None:
    """Corrupt the journal the way only a bug could: a row the real writer
    (apply_entry) would have refused, injected behind its back."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO ledger_entries (sleeve_id, entry_json, ts) VALUES (?,?,?)",
            (sleeve_id, entry.model_dump_json(), entry.ts.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def test_broken_fold_suppresses_numbers_and_stops(fixture_db, tmp_path):
    db, config = fixture_db
    _insert_raw_entry(
        db,
        "momentum",
        LedgerEntry(
            kind="fill", ts=NOW - timedelta(hours=1), symbol="EEM", side=Side.SELL,
            qty=Decimal("100"), price=Decimal("64.00"), fees=Decimal("0"),
        ),
    )
    _insert_raw_entry(
        db,
        "momentum",
        LedgerEntry(
            kind="fill", ts=NOW - timedelta(minutes=30), symbol="EEM", side=Side.BUY,
            qty=Decimal("1"), price=Decimal("64.00"), fees=Decimal("0"),
        ),
    )
    snap = build_snapshot(db, config, NOW)
    momentum = next(s for s in snap.sleeves if s.sleeve_id == "momentum")
    # The broken sleeve asserts nothing it cannot prove: no marks, no equity.
    assert momentum.fold_complete is False
    assert momentum.equity is None
    assert all(p.mark is None and p.open_pnl is None for p in momentum.positions)
    # The clean sleeve keeps its evidenced numbers.
    control = next(s for s in snap.sleeves if s.sleeve_id == "control")
    assert control.fold_complete is True and control.equity == Decimal("2501.75")
    assert any("fold violated an invariant" in gap for gap in snap.gaps)
    assert any(
        "Cannot explain 1 subsequent ledger entry for sleeve 'momentum'" in gap
        for gap in snap.gaps
    )
    md = render_markdown(snap)
    assert "**Ledger fold broke on this sleeve**" in md
    assert "| EEM | 15 | 65.88 | — | — | — |" in md


def test_malformed_entries_become_gaps_not_crashes(fixture_db, tmp_path):
    db, config = fixture_db
    # A fill with no side (apply() asserts) and a zero-qty buy (divides by
    # zero computing the new average): both must land as gaps, never a 500.
    _insert_raw_entry(
        db,
        "momentum",
        LedgerEntry(
            kind="fill", ts=NOW - timedelta(hours=1), symbol="EEM", side=None,
            qty=Decimal("1"), price=Decimal("64.00"), fees=Decimal("0"),
        ),
    )
    _insert_raw_entry(
        db,
        "control",
        LedgerEntry(
            # Zero-qty buy of a symbol not yet held: new_qty is 0 and the
            # average-cost computation divides by it.
            kind="fill", ts=NOW - timedelta(hours=1), symbol="QQQ", side=Side.BUY,
            qty=Decimal("0"), price=Decimal("770.00"), fees=Decimal("0"),
        ),
    )
    _path, md = generate(db, config, tmp_path / "reports", NOW)
    assert md.count("entry could not be applied") == 2
    # The narrative refuses to invent a direction for the side-less fill.
    assert "recorded a fill with no recorded side" in md
    assert "momentum sold" not in md


def test_serve_refuses_non_loopback_host(tmp_path):
    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code != 0
    assert "loopback" in result.output
    result = CliRunner().invoke(app, ["serve", "--host", "192.168.1.10"])
    assert result.exit_code != 0
    assert "loopback" in result.output
