from __future__ import annotations

from market_analysis.domain.errors import InstrumentNotSupportedError
from market_analysis.domain.models import Instrument
from market_analysis.infrastructure.providers.jin10.symbols import SPOT_GOLD, SPOT_SILVER


class Jin10WebSymbolMapper:
    def __init__(self) -> None:
        self._to_provider = {
            SPOT_GOLD: "XAUUSD.GOODS",
            SPOT_SILVER: "XAGUSD.GOODS",
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
