from pathlib import Path

import pytest

from nwt_engine.data import ParquetStore
from nwt_engine.data.fixtures import write_synthetic_fixture
from nwt_engine.experiments import (
    DataConfig,
    ExperimentConfig,
    InstrumentConfig,
    SleeveConfig,
)


@pytest.fixture(scope="session")
def fixture_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("parquet")
    write_synthetic_fixture(ParquetStore(root))
    return root


@pytest.fixture()
def buyhold_config(fixture_root: Path, tmp_path: Path) -> ExperimentConfig:
    return ExperimentConfig(
        id="test_buyhold",
        mode="backtest",
        data=DataConfig(provider="synthetic", root=fixture_root),
        instruments=[InstrumentConfig(symbol="SYNTH", asset_class="etf")],
        sleeves=[
            SleeveConfig(
                sleeve_id="control",
                strategy="buyhold",
                capital="10000",
                params={"symbol": "SYNTH"},
            )
        ],
        results_db=tmp_path / "results.db",
    )
