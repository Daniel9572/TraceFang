from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from market_analysis.application.ports import CandleProvider, QuoteProvider
from market_analysis.domain.errors import ProviderChainExhaustedError, ProviderError
from market_analysis.domain.models import Candle, Instrument, QuoteSnapshot


class MarketDataService:
    """Routes domain requests through ordered, replaceable provider chains."""

    def __init__(
        self,
        *,
        quote_providers: Sequence[QuoteProvider],
        candle_providers: Sequence[CandleProvider],
    ) -> None:
        if not quote_providers:
            raise ValueError("at least one quote provider is required")
        if not candle_providers:
            raise ValueError("at least one candle provider is required")
        self._quote_providers = tuple(quote_providers)
        self._candle_providers = tuple(candle_providers)

    async def get_quote(self, instrument: Instrument) -> QuoteSnapshot:
        failures: list[tuple[str, ProviderError]] = []
        for provider in self._quote_providers:
            try:
                return await provider.get_quote(instrument)
            except ProviderError as error:
                failures.append((provider.name, error))
        raise ProviderChainExhaustedError("quote", failures)

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        failures: list[tuple[str, ProviderError]] = []
        for provider in self._candle_providers:
            try:
                return await provider.get_candles(instrument, start=start, count=count)
            except ProviderError as error:
                failures.append((provider.name, error))
        raise ProviderChainExhaustedError("candle", failures)
