from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from market_analysis.domain.errors import ProviderUnavailableError
from market_analysis.domain.models import Instrument, QuoteSnapshot, SourceMetadata

JIN10_CLIENT_SOURCE = "jin10_client"
JIN10_WEB_CHANNEL = "jin10_web"
JIN10_LOCAL_CHANNEL = "jin10_local"

_PRICE_FIELDS = ("last", "change", "change_percent")
_SUPPLEMENT_FIELDS = ("open", "high", "low", "volume")


class QuoteQuality(StrEnum):
    COMPLETE = "complete"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class LogicalQuoteSnapshot:
    """Aggregated presentation values; never persisted as raw channel evidence."""

    instrument: Instrument
    last: Decimal
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    volume: Decimal | None
    change: Decimal | None
    change_percent: Decimal | None
    source: SourceMetadata


@dataclass(frozen=True, slots=True)
class QuoteView:
    """One logical source result with its physical composition fully encapsulated."""

    source_id: str
    quote: LogicalQuoteSnapshot
    quality: QuoteQuality
    unavailable_fields: tuple[str, ...]
    stale_fields: tuple[str, ...]
    composed_at: datetime


LatestQuoteLoader = Callable[[Instrument, str], Awaitable[QuoteSnapshot | None]]
StaleAfter = Callable[[str], float]
_QuoteKey = tuple[str, str]


class LatestQuoteCache:
    """In-memory hot cache backed only by the local quote store."""

    def __init__(self, loader: LatestQuoteLoader) -> None:
        self._loader = loader
        self._values: dict[_QuoteKey, QuoteSnapshot] = {}
        self._locks: dict[_QuoteKey, asyncio.Lock] = {}

    def put(self, quote: QuoteSnapshot) -> bool:
        key = (quote.source.provider, quote.instrument.symbol)
        current = self._values.get(key)
        if current == quote:
            return False
        if current is not None and quote.source.received_at < current.source.received_at:
            return False
        self._values[key] = quote
        return True

    def peek(self, instrument: Instrument, source_id: str) -> QuoteSnapshot | None:
        return self._values.get((source_id, instrument.symbol))

    async def get(self, instrument: Instrument, source_id: str) -> QuoteSnapshot | None:
        key = (source_id, instrument.symbol)
        cached = self._values.get(key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._values.get(key)
            if cached is not None:
                return cached
            loaded = await self._loader(instrument, source_id)
            if loaded is not None:
                self.put(loaded)
            return loaded

    def clear(self) -> None:
        self._values.clear()
        self._locks.clear()


class QuoteViewService:
    """Builds an encapsulated logical-source result from the local hot cache."""

    def __init__(self, cache: LatestQuoteCache, *, stale_after: StaleAfter) -> None:
        self._cache = cache
        self._stale_after = stale_after

    def accept(self, quote: QuoteSnapshot) -> bool:
        return self._cache.put(quote)

    async def get(self, instrument: Instrument, source_id: str) -> QuoteView:
        self._require_logical_source(source_id)
        price = await self._cache.get(instrument, JIN10_WEB_CHANNEL)
        supplement = await self._cache.get(instrument, JIN10_LOCAL_CHANNEL)
        return self._compose_client(instrument, price, supplement)

    def build_cached(self, instrument: Instrument, source_id: str) -> QuoteView:
        self._require_logical_source(source_id)
        return self._compose_client(
            instrument,
            self._cache.peek(instrument, JIN10_WEB_CHANNEL),
            self._cache.peek(instrument, JIN10_LOCAL_CHANNEL),
        )

    @staticmethod
    def _require_logical_source(source_id: str) -> None:
        if source_id != JIN10_CLIENT_SOURCE:
            raise ProviderUnavailableError(f"{source_id} is not a selectable logical quote source")

    def _compose_client(
        self,
        instrument: Instrument,
        price: QuoteSnapshot | None,
        supplement: QuoteSnapshot | None,
    ) -> QuoteView:
        if price is None:
            raise ProviderUnavailableError("金十客户端行情暂时没有实时价格")
        if self._is_stale(JIN10_WEB_CHANNEL, price):
            raise ProviderUnavailableError("金十客户端行情的实时价格已过期")

        unavailable_fields = [
            field_name for field_name in _PRICE_FIELDS if getattr(price, field_name) is None
        ]
        stale_fields: list[str] = []
        supplement_values: dict[str, Decimal | None] = {
            field_name: None for field_name in _SUPPLEMENT_FIELDS
        }
        if supplement is None:
            unavailable_fields.extend(_SUPPLEMENT_FIELDS)
        elif self._is_stale(JIN10_LOCAL_CHANNEL, supplement):
            stale_fields.extend(_SUPPLEMENT_FIELDS)
        else:
            for field_name in _SUPPLEMENT_FIELDS:
                value = getattr(supplement, field_name)
                supplement_values[field_name] = value
                if value is None:
                    unavailable_fields.append(field_name)

        logical_quote = LogicalQuoteSnapshot(
            instrument=instrument,
            last=price.last,
            open=supplement_values["open"],
            high=supplement_values["high"],
            low=supplement_values["low"],
            volume=supplement_values["volume"],
            change=price.change,
            change_percent=price.change_percent,
            source=SourceMetadata(
                provider=JIN10_CLIENT_SOURCE,
                provider_symbol=instrument.symbol,
                observed_at=price.source.observed_at,
                received_at=price.source.received_at,
            ),
        )
        unavailable = tuple(dict.fromkeys(unavailable_fields))
        stale = tuple(dict.fromkeys(stale_fields))
        return QuoteView(
            source_id=JIN10_CLIENT_SOURCE,
            quote=logical_quote,
            quality=(QuoteQuality.DEGRADED if unavailable or stale else QuoteQuality.COMPLETE),
            unavailable_fields=unavailable,
            stale_fields=stale,
            composed_at=datetime.now(UTC),
        )

    def _is_stale(self, source_id: str, quote: QuoteSnapshot) -> bool:
        age = max(0.0, (datetime.now(UTC) - quote.source.received_at).total_seconds())
        return age > self._stale_after(source_id)
