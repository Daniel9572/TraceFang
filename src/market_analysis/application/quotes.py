from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from market_analysis.domain.errors import ProviderUnavailableError
from market_analysis.domain.models import Instrument, QuoteSnapshot

JIN10_CLIENT_SOURCE = "jin10_client"
JIN10_WEB_CHANNEL = "jin10_web"
JIN10_LOCAL_CHANNEL = "jin10_local"

_PRICE_FIELDS = ("last", "change", "change_percent")
_SUPPLEMENT_FIELDS = ("open", "high", "low", "volume")
_ALL_QUOTE_FIELDS = _PRICE_FIELDS + _SUPPLEMENT_FIELDS


@dataclass(frozen=True, slots=True)
class QuoteComponent:
    """One raw channel participating in a logical quote view."""

    source_id: str
    role: str
    fields: tuple[str, ...]
    available: bool
    stale: bool
    age_seconds: float | None
    quote: QuoteSnapshot | None


@dataclass(frozen=True, slots=True)
class QuoteView:
    """Presentation/query view that never masquerades as a raw source record."""

    source_id: str
    price: QuoteSnapshot
    supplement: QuoteSnapshot | None
    field_sources: Mapping[str, str]
    components: tuple[QuoteComponent, ...]
    missing_channels: tuple[str, ...]
    stale_channels: tuple[str, ...]
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
    """Builds channel-pure or explicitly composed views from the local cache."""

    def __init__(self, cache: LatestQuoteCache, *, stale_after: StaleAfter) -> None:
        self._cache = cache
        self._stale_after = stale_after

    def accept(self, quote: QuoteSnapshot) -> bool:
        return self._cache.put(quote)

    async def get(self, instrument: Instrument, source_id: str) -> QuoteView:
        if source_id == JIN10_CLIENT_SOURCE:
            price = await self._cache.get(instrument, JIN10_WEB_CHANNEL)
            supplement = await self._cache.get(instrument, JIN10_LOCAL_CHANNEL)
            return self._compose_client(instrument, price, supplement)
        quote = await self._cache.get(instrument, source_id)
        return self._single_channel(instrument, source_id, quote)

    def build_cached(self, instrument: Instrument, source_id: str) -> QuoteView:
        if source_id == JIN10_CLIENT_SOURCE:
            return self._compose_client(
                instrument,
                self._cache.peek(instrument, JIN10_WEB_CHANNEL),
                self._cache.peek(instrument, JIN10_LOCAL_CHANNEL),
            )
        return self._single_channel(
            instrument,
            source_id,
            self._cache.peek(instrument, source_id),
        )

    def _single_channel(
        self,
        instrument: Instrument,
        source_id: str,
        quote: QuoteSnapshot | None,
    ) -> QuoteView:
        component = self._component(
            source_id,
            role="complete_quote",
            fields=_ALL_QUOTE_FIELDS,
            quote=quote,
        )
        if quote is None:
            raise ProviderUnavailableError(f"{source_id} has no locally cached quote")
        if component.stale:
            raise ProviderUnavailableError(f"{source_id} locally cached quote is stale")
        return QuoteView(
            source_id=source_id,
            price=quote,
            supplement=None,
            field_sources={field: source_id for field in _ALL_QUOTE_FIELDS},
            components=(component,),
            missing_channels=(),
            stale_channels=(),
            composed_at=datetime.now(UTC),
        )

    def _compose_client(
        self,
        instrument: Instrument,
        price: QuoteSnapshot | None,
        supplement: QuoteSnapshot | None,
    ) -> QuoteView:
        price_component = self._component(
            JIN10_WEB_CHANNEL,
            role="realtime_price",
            fields=_PRICE_FIELDS,
            quote=price,
        )
        supplement_component = self._component(
            JIN10_LOCAL_CHANNEL,
            role="session_supplement",
            fields=_SUPPLEMENT_FIELDS,
            quote=supplement,
        )
        if price is None:
            raise ProviderUnavailableError(
                "金十客户端组合行情缺少 jin10_web 实时价格通道"
            )
        if price_component.stale:
            raise ProviderUnavailableError(
                "金十客户端组合行情的 jin10_web 实时价格已过期"
            )
        components = (price_component, supplement_component)
        return QuoteView(
            source_id=JIN10_CLIENT_SOURCE,
            price=price,
            supplement=supplement,
            field_sources={
                **{field: JIN10_WEB_CHANNEL for field in _PRICE_FIELDS},
                **{field: JIN10_LOCAL_CHANNEL for field in _SUPPLEMENT_FIELDS},
            },
            components=components,
            missing_channels=tuple(
                component.source_id for component in components if not component.available
            ),
            stale_channels=tuple(
                component.source_id for component in components if component.stale
            ),
            composed_at=datetime.now(UTC),
        )

    def _component(
        self,
        source_id: str,
        *,
        role: str,
        fields: tuple[str, ...],
        quote: QuoteSnapshot | None,
    ) -> QuoteComponent:
        if quote is None:
            return QuoteComponent(
                source_id=source_id,
                role=role,
                fields=fields,
                available=False,
                stale=False,
                age_seconds=None,
                quote=None,
            )
        age = max(0.0, (datetime.now(UTC) - quote.source.received_at).total_seconds())
        return QuoteComponent(
            source_id=source_id,
            role=role,
            fields=fields,
            available=True,
            stale=age > self._stale_after(source_id),
            age_seconds=age,
            quote=quote,
        )
