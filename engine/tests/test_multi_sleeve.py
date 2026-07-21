"""End-to-end multi-sleeve backtest: netting, reconciliation, relative metrics,
and determinism all exercised through the real runner on synthetic data.

Reconciliation is the assertion that matters most here: the runner raises if
the sum of sleeve ledgers ever drifts from the SimBroker account by more than
a cent — so a completed run IS the conservation proof, crosses included.
"""

from pathlib import Path

import pytest

from nwt_engine.data import ParquetStore
from nwt_engine.data.fixtures import write_synthetic_fixture
from nwt_engine.experiments import (
    BacktestRunner,
    DataConfig,
    ExperimentConfig,
    InstrumentConfig,
    SleeveConfig,
)


@pytest.fixture(scope="module")
def multi_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("parquet-multi")
    store = ParquetStore(root)
    # Three symbols with different seeds/drifts so momentum has real dispersion.
    write_synthetic_fixture(store, symbol="AAA", seed=11)
    write_synthetic_fixture(store, symbol="BBB", seed=23)
    write_synthetic_fixture(store, symbol="CCC", seed=47)
    return root


def _config(root: Path, db: Path) -> ExperimentConfig:
    symbols = ["AAA", "BBB", "CCC"]
    return ExperimentConfig(
        id="test_multi_sleeve",
        mode="backtest",
        data=DataConfig(provider="synthetic", root=root),
        instruments=[InstrumentConfig(symbol=s, asset_class="etf") for s in symbols],
        control_sleeve="control",
        sleeves=[
            SleeveConfig(
                sleeve_id="control",
                strategy="buyhold",
                capital="4000",
                params={"symbol": "AAA"},
            ),
            SleeveConfig(
                sleeve_id="mom",
                strategy="momentum_rotation",
                capital="4000",
                params={"symbols": symbols, "n": 2, "lookback": 120, "skip": 21},
            ),
            SleeveConfig(
                sleeve_id="mr",
                strategy="meanrev_zscore",
                capital="2000",
                params={"symbols": symbols, "lookback": 20, "max_positions": 2},
            ),
        ],
        results_db=db,
    )


def test_multi_sleeve_run_reconciles_and_reports(multi_root: Path, tmp_path: Path):
    result = BacktestRunner(_config(multi_root, tmp_path / "r.db")).run()

    assert {s.sleeve_id for s in result.sleeves} == {"control", "mom", "mr"}
    by_id = {s.sleeve_id: s for s in result.sleeves}

    # Control gets absolute metrics only; others get benchmark-relative ones too.
    assert "beta_vs_control" not in by_id["control"].metrics
    for sleeve_id in ("mom", "mr"):
        metrics = by_id[sleeve_id].metrics
        assert "beta_vs_control" in metrics
        assert "information_ratio" in metrics
        assert "turnover_annualized" in metrics
        assert metrics["turnover_annualized"] >= 0.0

    # Sleeves traded: equity ended somewhere real, not stuck at initial capital.
    assert by_id["mom"].final_equity != 4000


def test_multi_sleeve_determinism(multi_root: Path, tmp_path: Path):
    first = BacktestRunner(_config(multi_root, tmp_path / "a.db")).run()
    second = BacktestRunner(_config(multi_root, tmp_path / "b.db")).run()
    assert first.journal_hash == second.journal_hash
    assert [s.final_equity for s in first.sleeves] == [
        s.final_equity for s in second.sleeves
    ]


def test_midnight_stamped_bars_still_fill(tmp_path):
    """Regression: Alpaca-convention daily bars (ts_close(N) == ts_open(N+1))
    must not silently starve fills. Root cause of the flat-control bug found
    in the first real-data rewind (2026-07-21)."""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from nwt_engine.domain import Bar, Timeframe

    store = ParquetStore(tmp_path / "pq")
    bars = []
    ts = datetime(2024, 1, 1, 5, tzinfo=UTC)
    price = Decimal("100.00")
    for _ in range(10):
        bars.append(
            Bar(
                symbol="MIDNIGHT",
                timeframe=Timeframe.D1,
                ts_open=ts,
                ts_close=ts + timedelta(days=1),  # == next bar's ts_open
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=Decimal("1000000"),
            )
        )
        ts += timedelta(days=1)
    store.write_bars("synthetic", Timeframe.D1, "MIDNIGHT", bars)

    cfg = ExperimentConfig(
        id="test_midnight",
        mode="backtest",
        data=DataConfig(provider="synthetic", root=tmp_path / "pq"),
        instruments=[InstrumentConfig(symbol="MIDNIGHT", asset_class="etf")],
        sleeves=[
            SleeveConfig(
                sleeve_id="control",
                strategy="buyhold",
                capital="10000",
                params={"symbol": "MIDNIGHT"},
            )
        ],
        results_db=tmp_path / "r.db",
    )
    result = BacktestRunner(cfg).run()
    control = result.sleeves[0]
    # The one-shot buyhold order must have filled: equity is invested, not cash.
    assert control.final_equity != Decimal("10000")
