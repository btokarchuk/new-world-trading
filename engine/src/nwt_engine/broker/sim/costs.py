"""Cost models. Deliberately conservative: Alpaca paper fills simulate no
slippage at all, so honest evaluation lives here, not at the broker."""

from decimal import Decimal

from pydantic import BaseModel

from nwt_contracts import AssetClass, Side

_BPS = Decimal("0.0001")


class CostModel(BaseModel, frozen=True):
    equity_slippage_bps: Decimal = Decimal("5")     # liquid ETFs default
    crypto_slippage_bps: Decimal = Decimal("20")
    equity_commission: Decimal = Decimal("0")       # Alpaca: $0
    crypto_taker_pct: Decimal = Decimal("0.25")     # always assume taker

    def slip_price(self, asset_class: AssetClass, side: Side, price: Decimal) -> Decimal:
        bps = (
            self.crypto_slippage_bps
            if asset_class is AssetClass.CRYPTO
            else self.equity_slippage_bps
        )
        adverse = bps * _BPS * price
        return price + adverse if side is Side.BUY else price - adverse

    def fees(self, asset_class: AssetClass, qty: Decimal, price: Decimal) -> Decimal:
        if asset_class is AssetClass.CRYPTO:
            return (qty * price * self.crypto_taker_pct / Decimal("100")).quantize(
                Decimal("0.0001")
            )
        return self.equity_commission
