from __future__ import annotations

from tracefang.domain.errors import InstrumentNotSupportedError
from tracefang.domain.models import Instrument
from tracefang.instruments import SPOT_GOLD, SPOT_SILVER, USD_CNH


class Jin10WebSymbolMapper:
    def __init__(self) -> None:
        self._to_provider = {
            SPOT_GOLD: "XAUUSD.GOODS",
            SPOT_SILVER: "XAGUSD.GOODS",
            USD_CNH: "USDCNH.FXCM",
        }
        self._from_provider = {value: key for key, value in self._to_provider.items()}

    @property
    def provider_codes(self) -> tuple[str, ...]:
        return tuple(self._from_provider)

    def to_provider_code(self, instrument: Instrument) -> str:
        try:
            return self._to_provider[instrument]
        except KeyError as error:
            raise InstrumentNotSupportedError(
                f"jin10 web has no configured mapping for {instrument.symbol}"
            ) from error

    def from_provider_code(self, code: str) -> Instrument:
        try:
            return self._from_provider[code.upper()]
        except KeyError as error:
            raise InstrumentNotSupportedError(
                f"jin10 web code {code!r} has no canonical mapping"
            ) from error
