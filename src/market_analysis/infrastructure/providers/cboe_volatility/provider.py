from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from market_analysis.domain.errors import ProviderDataError, ProviderUnavailableError
from market_analysis.domain.market_context import (
    EndOfDayMarketContextSource,
    MarketContextSource,
    VolatilityIndexContext,
    VolatilityIndexEodContext,
)
from market_analysis.infrastructure.providers.cboe_volatility.protocol import (
    CboeDelayedQuote,
    CboeHistoryPoint,
    parse_delayed_quote,
    parse_history_csv,
    trailing_percentile,
)
from market_analysis.infrastructure.providers.cboe_volatility.settings import (
    CboeVolatilitySettings,
)

_INDEX_UNDERLYINGS = {"VIX": "SPX", "GVZ": "GLD"}
_CHICAGO = ZoneInfo("America/Chicago")


@dataclass(frozen=True, slots=True)
class _QuoteCacheEntry:
    expires_at: float
    quote: CboeDelayedQuote
    received_at: datetime


@dataclass(frozen=True, slots=True)
class _HistoryCacheEntry:
    expires_at: float
    points: tuple[CboeHistoryPoint, ...]
    received_at: datetime


class CboeVolatilityProvider:
    """Reads Cboe delayed-index assets without registering a runtime source.

    Cboe publishes the history CSV files for download, while automated delayed-quote access
    requires a separate usage/licensing review before this adapter may be polled in production.
    """

    name = "cboe_volatility"

    def __init__(
        self,
        settings: CboeVolatilitySettings | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings or CboeVolatilitySettings()
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=self.settings.timeout_seconds)
        self._monotonic = monotonic_clock
        self._utc_clock = utc_clock
        self._quote_cache: dict[str, _QuoteCacheEntry] = {}
        self._history_cache: dict[str, _HistoryCacheEntry] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def get_context(self, index_code: str) -> VolatilityIndexContext:
        normalized = index_code.strip().upper()
        try:
            underlying = _INDEX_UNDERLYINGS[normalized]
        except KeyError as error:
            raise ValueError(f"unsupported Cboe volatility index {index_code!r}") from error
        quote_entry = await self._quote(normalized)
        history = await self._history(normalized)
        percentile, sample = trailing_percentile(
            history,
            value=quote_entry.quote.value,
            before=quote_entry.quote.observed_at.astimezone(_CHICAGO).date(),
        )
        source = MarketContextSource(
            provider_id=self.name,
            dataset_id=f"cboe-delayed-{normalized.lower()}",
            source_url=self.settings.quote_url(normalized),
            observed_at=quote_entry.quote.observed_at,
            received_at=quote_entry.received_at,
            published_at=quote_entry.quote.published_at,
            delayed=True,
            declared_delay=timedelta(minutes=15),
        )
        return VolatilityIndexContext(
            index_code=normalized,
            underlying=underlying,
            value=quote_entry.quote.value,
            change=quote_entry.quote.change,
            change_percent=quote_entry.quote.change_percent,
            session_open=quote_entry.quote.session_open,
            session_high=quote_entry.quote.session_high,
            session_low=quote_entry.quote.session_low,
            previous_close=quote_entry.quote.previous_close,
            trailing_percentile_252=percentile,
            history_sample_size=len(sample),
            history_start=sample[0].trading_date if sample else None,
            history_end=sample[-1].trading_date if sample else None,
            source=source,
        )

    async def get_eod_context(self, index_code: str) -> VolatilityIndexEodContext:
        """Return the latest published history row without reading delayed quote assets."""

        normalized = index_code.strip().upper()
        try:
            underlying = _INDEX_UNDERLYINGS[normalized]
        except KeyError as error:
            raise ValueError(f"unsupported Cboe volatility index {index_code!r}") from error
        history_entry = await self._history_entry(normalized)
        latest = history_entry.points[-1]
        percentile, sample = trailing_percentile(
            history_entry.points,
            value=latest.close,
            before=latest.trading_date + timedelta(days=1),
        )
        return VolatilityIndexEodContext(
            index_code=normalized,
            underlying=underlying,
            value=latest.close,
            trailing_percentile_252=percentile,
            history_sample_size=len(sample),
            history_start=sample[0].trading_date if sample else None,
            history_end=sample[-1].trading_date if sample else None,
            source=EndOfDayMarketContextSource(
                provider_id=self.name,
                dataset_id=f"{normalized}_History.csv",
                source_url=self.settings.history_url(normalized),
                as_of=latest.trading_date,
                received_at=history_entry.received_at,
            ),
        )

    async def _quote(self, index_code: str) -> _QuoteCacheEntry:
        async with self._lock:
            now = self._monotonic()
            cached = self._quote_cache.get(index_code)
            if cached is not None and cached.expires_at > now:
                return cached
            payload = await self._get_json(self.settings.quote_url(index_code))
            quote = parse_delayed_quote(payload, expected_index_code=index_code)
            entry = _QuoteCacheEntry(
                expires_at=now + self.settings.quote_cache_ttl_seconds,
                quote=quote,
                received_at=self._utc_clock().astimezone(UTC),
            )
            self._quote_cache[index_code] = entry
            return entry

    async def _history(self, index_code: str) -> tuple[CboeHistoryPoint, ...]:
        return (await self._history_entry(index_code)).points

    async def _history_entry(self, index_code: str) -> _HistoryCacheEntry:
        async with self._lock:
            now = self._monotonic()
            cached = self._history_cache.get(index_code)
            if cached is not None and cached.expires_at > now:
                return cached
            text = await self._get_text(self.settings.history_url(index_code))
            points = parse_history_csv(text, expected_index_code=index_code)
            entry = _HistoryCacheEntry(
                expires_at=now + self.settings.history_cache_ttl_seconds,
                points=points,
                received_at=self._utc_clock().astimezone(UTC),
            )
            self._history_cache[index_code] = entry
            return entry

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url, timeout=self.settings.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            raise ProviderUnavailableError("Cboe delayed volatility request failed") from error
        except ValueError as error:
            raise ProviderDataError("Cboe delayed volatility response is invalid JSON") from error
        if not isinstance(payload, dict):
            raise ProviderDataError("Cboe delayed volatility response has an invalid root")
        return payload

    async def _get_text(self, url: str) -> str:
        try:
            response = await self._http.get(url, timeout=self.settings.timeout_seconds)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ProviderUnavailableError("Cboe volatility history request failed") from error
        return response.text
