"""Golden walking-skeleton test.

Runs buy-and-hold on the deterministic synthetic fixture and verifies the
engine's accounting with an INDEPENDENT reconstruction from raw DB rows and
fixture data — fill price, dividend credits, and final equity must match to
the cent. Also proves determinism: two runs produce identical journal hashes.
"""

import sqlite3
from decimal import Decimal

from nwt_engine.data.fixtures import generate_synthetic
from nwt_engine.experiments import BacktestRunner


def test_buyhold_accounting_and_determinism(buyhold_config):
    result = BacktestRunner(buyhold_config).run()

    bars, actions = generate_synthetic()
    conn = sqlite3.connect(buyhold_config.results_db)
    fills = conn.execute(
        "SELECT symbol, side, qty, price, fees, ts FROM fills WHERE run_id = ?",
        (result.run_id,),
    ).fetchall()

    # Exactly one entry fill: buy on day 1's close decision, filled at day 2's open.
    assert len(fills) == 1
    symbol, side, qty_s, price_s, fees_s, fill_ts = fills[0]
    assert (symbol, side) == ("SYNTH", "buy")
    qty, price, fees = Decimal(qty_s), Decimal(price_s), Decimal(fees_s)

    # Fill-price pinning: next-bar open + 5bps adverse slippage, capped at the
    # marketable limit (day-1 close * 1.005), quantized to the cent grid used
    # by SimBroker (4dp).
    day1_close, day2_open = bars[0].close, bars[1].open
    slipped = day2_open * (Decimal("1") + Decimal("5") * Decimal("0.0001"))
    limit = (day1_close * Decimal("1.005")).quantize(Decimal("0.01"))
    expected_price = min(slipped, limit).quantize(Decimal("0.0001"))
    assert price == expected_price
    assert fees == 0  # equities are commission-free

    # Sizing pinning: floor(equity / buy-limit) whole shares — sized against the
    # worst-case fill so full weight is always affordable.
    assert qty == (Decimal("10000") / limit).to_integral_value(rounding="ROUND_FLOOR")

    # Independent equity reconstruction: cash after entry + dividends while held.
    entry_ts = fills[0][5]
    dividends = sum(
        (qty * a.cash for a in actions if a.kind == "dividend" and a.ex_date.isoformat() > entry_ts),
        Decimal("0"),
    )
    expected_cash = Decimal("10000") - qty * price - fees + dividends
    expected_equity = expected_cash + qty * bars[-1].close

    sleeve = result.sleeves[0]
    assert sleeve.final_equity == expected_equity

    (db_cash, db_equity) = conn.execute(
        "SELECT cash, equity FROM equity_daily WHERE run_id=? ORDER BY ts DESC LIMIT 1",
        (result.run_id,),
    ).fetchone()
    assert Decimal(db_cash) == expected_cash
    assert Decimal(db_equity) == expected_equity
    conn.close()

    # Metrics exist and are sane.
    assert set(sleeve.metrics) >= {"total_return", "cagr", "sharpe", "max_drawdown"}
    assert sleeve.metrics["max_drawdown"] >= 0

    # Determinism: an identical second run yields a byte-identical journal.
    second = BacktestRunner(buyhold_config).run()
    assert second.journal_hash == result.journal_hash
    assert second.run_id == result.run_id


def test_reconcile_runs_every_close(buyhold_config):
    result = BacktestRunner(buyhold_config).run()
    conn = sqlite3.connect(buyhold_config.results_db)
    n_reconciles = conn.execute(
        "SELECT COUNT(*) FROM events WHERE run_id=? AND type='reconcile'", (result.run_id,)
    ).fetchone()[0]
    n_bars = conn.execute(
        "SELECT COUNT(*) FROM events WHERE run_id=? AND type='bar'", (result.run_id,)
    ).fetchone()[0]
    conn.close()
    assert n_reconciles == n_bars  # single symbol: one decision point per bar
    assert n_reconciles > 400  # two years of sessions
