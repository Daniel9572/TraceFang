from __future__ import annotations

from market_analysis.domain.errors import InstrumentNotSupportedError
from market_analysis.domain.models import AssetClass, Instrument

SPOT_GOLD = Instrument(
    symbol="XAU/USD",
    asset_class=AssetClass.SPOT,
    base="XAU",
    quote="USD",
    venue="OTC",
)
SPOT_SILVER = Instrument(
    symbol="XAG/USD",
    asset_class=AssetClass.SPOT,
    base="XAG",
    quote="USD",
    venue="OTC",
)


class Jin10SymbolMapper:
    def __init__(self) -> None:
        self._to_provider = {
            SPOT_GOLD: "XAUUSD",
            SPOT_SILVER: "XAGUSD",
        }
        self._from_provider = {value: key for key, value in self._to_provider.items()}

    def to_provider_code(self, instrument: Instrument) -> str:
        try:
            return self._to_provider[instrument]
        except KeyError as error:
            raise InstrumentNotSupportedError(
                f"jin10 has no configured mapping for {instrument.symbol}"
            ) from error

    def from_provider_code(self, code: str) -> Instrument:
        try:
            return self._from_provider[code.upper()]
        except KeyError as error:
            raise InstrumentNotSupportedError(
                f"jin10 code {code!r} has no canonical mapping"
            ) from error

    def known_mapping(self, code: str) -> Instrument | None:
        return self._from_provider.get(code.upper())
