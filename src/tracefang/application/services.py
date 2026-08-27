from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from tracefang.application.ports import CandleProvider, QuoteProvider
from tracefang.domain.errors import ProviderUnavailableError
from tracefang.domain.models import Candle, Instrument, QuoteSnapshot


class MarketDataService:
    """Routes each request to one explicitly selected provider without fallback."""

    def __init__(
        self,
        *,
        quote_providers: Mapping[str, QuoteProvider],
        candle_providers: Mapping[str, CandleProvider],
    ) -> None:
        if not quote_providers:
            raise ValueError("at least one quote provider is required")
        if not candle_providers:
            raise ValueError("at least one candle provider is required")
        self._quote_providers = dict(quote_providers)
        self._candle_providers = dict(candle_providers)

    async def get_quote(self, instrument: Instrument, *, source: str) -> QuoteSnapshot:
        provider = self._quote_providers.get(source)
        if provider is None:
            raise ProviderUnavailableError(f"quote source {source!r} is unavailable")
        return await provider.get_quote(instrument)

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        source: str,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        provider = self._candle_providers.get(source)
        if provider is None:
            raise ProviderUnavailableError(f"candle source {source!r} is unavailable")
        return await provider.get_candles(instrument, start=start, count=count)
