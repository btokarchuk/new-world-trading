from decimal import Decimal

from pydantic import BaseModel

from nwt_contracts import AssetClass


class Instrument(BaseModel, frozen=True):
    symbol: str                   # canonical: "SPY", "BTC/USD"
    asset_class: AssetClass
    calendar: str                 # "XNYS" | "24_7"
    tick_size: Decimal = Decimal("0.01")
    fractionable: bool = False
    min_notional: Decimal = Decimal("1")


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
