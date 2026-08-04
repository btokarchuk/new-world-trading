from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from nwt_contracts import AssetClass


class Instrument(BaseModel, frozen=True):
    symbol: str                   # canonical: "SPY", "BTC/USD"
    asset_class: AssetClass
    calendar: str                 # "XNYS" | "24_7"
    tick_size: Decimal = Decimal("0.01")
    fractionable: bool = False
    min_notional: Decimal = Decimal("1")
    # Smallest tradeable quantity step. Whole shares for equities; Alpaca's
    # crypto minimum order size AND increment is 0.0001 for BTC/ETH. Sizing a
    # $750 crypto sleeve against a whole-unit step floors it to zero forever,
    # which is why the crypto sleeve never deployed.
    qty_increment: Decimal = Decimal("1")
    # Crypto accepts only gtc/ioc — a `day` order is rejected outright.
    tif: Literal["day", "ioc", "gtc"] = "day"


class Universe(BaseModel, frozen=True):
    name: str
    instruments: tuple[Instrument, ...]

    def get(self, symbol: str) -> Instrument:
        for inst in self.instruments:
            if inst.symbol == symbol:
                return inst
        raise KeyError(symbol)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(i.symbol for i in self.instruments)
