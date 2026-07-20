from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from nwt_contracts import AssetClass

from nwt_engine.broker import CostModel
from nwt_engine.domain import Instrument, Timeframe, Universe


class InstrumentConfig(BaseModel):
    symbol: str
    asset_class: AssetClass
    calendar: str = "XNYS"
    tick_size: Decimal = Decimal("0.01")
    fractionable: bool = False

    def to_instrument(self) -> Instrument:
        return Instrument(
            symbol=self.symbol,
            asset_class=self.asset_class,
            calendar=self.calendar,
            tick_size=self.tick_size,
            fractionable=self.fractionable,
        )


class DataConfig(BaseModel):
    provider: str = "synthetic"
    timeframe: Timeframe = Timeframe.D1
    root: Path = Path("data/parquet")


class SleeveConfig(BaseModel):
    sleeve_id: str
    strategy: str
    capital: Decimal
    params: dict = {}


class ExperimentConfig(BaseModel):
    id: str
    mode: Literal["backtest", "paper", "live"] = "backtest"
    data: DataConfig
    costs: CostModel = CostModel()
    instruments: list[InstrumentConfig]
    sleeves: list[SleeveConfig]
    results_db: Path = Path("data/results.db")

    @property
    def universe(self) -> Universe:
        return Universe(
            name=self.id,
            instruments=tuple(i.to_instrument() for i in self.instruments),
        )

    @classmethod
    def load(cls, path: Path | str) -> "ExperimentConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls.model_validate(raw)
