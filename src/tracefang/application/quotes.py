from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from tracefang.domain.errors import ProviderUnavailableError
from tracefang.domain.models import Instrument, QuoteSnapshot, SourceMetadata
from tracefang.instruments import (
    SPOT_GOLD,
    SPOT_GOLD_CNH_PER_GRAM,
    TROY_OUNCE_GRAMS,
    USD_CNH,
)

JIN10_CLIENT_SOURCE = "jin10_client"
JIN10_WEB_CHANNEL = "jin10_web"
JIN10_LOCAL_CHANNEL = "jin10_local"
TONGHUASHUN_FUTURES_SOURCE = "tonghuashun_futures"

_PRICE_FIELDS = ("last", "change", "change_percent")
_SUPPLEMENT_FIELDS = ("open", "high", "low", "volume")


class QuoteQuality(StrEnum):
    COMPLETE = "complete"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class RealtimeQuoteSnapshot:
    """One realtime source's presentation values; never persisted as raw evidence."""

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
    """One realtime source result with its internal composition fully encapsulated."""

    source_id: str
    quote: RealtimeQuoteSnapshot
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
    """Builds an encapsulated realtime-source result from the local hot cache."""

    def __init__(self, cache: LatestQuoteCache, *, stale_after: StaleAfter) -> None:
        self._cache = cache
        self._stale_after = stale_after

    def accept(self, quote: QuoteSnapshot) -> bool:
        return self._cache.put(quote)

    async def get(self, instrument: Instrument, source_id: str) -> QuoteView:
        self._require_realtime_source(source_id)
        if source_id == TONGHUASHUN_FUTURES_SOURCE:
            return self._compose_direct(
                instrument,
                source_id,
                await self._cache.get(instrument, source_id),
            )
        if instrument == SPOT_GOLD_CNH_PER_GRAM:
            return self._compose_derived_gold(
                await self._cache.get(SPOT_GOLD, JIN10_WEB_CHANNEL),
                await self._cache.get(USD_CNH, JIN10_WEB_CHANNEL),
            )
        price = await self._cache.get(instrument, JIN10_WEB_CHANNEL)
        supplement = await self._cache.get(instrument, JIN10_LOCAL_CHANNEL)
        return self._compose_client(instrument, price, supplement)

    async def get_last(self, instrument: Instrument, source_id: str) -> QuoteView:
        """Return the same-source last snapshot while preserving explicit staleness."""

        self._require_realtime_source(source_id)
        if source_id == TONGHUASHUN_FUTURES_SOURCE:
            return self._compose_direct(
                instrument,
                source_id,
                await self._cache.get(instrument, source_id),
                allow_stale=True,
            )
        if instrument == SPOT_GOLD_CNH_PER_GRAM:
            return self._compose_derived_gold(
                await self._cache.get(SPOT_GOLD, JIN10_WEB_CHANNEL),
                await self._cache.get(USD_CNH, JIN10_WEB_CHANNEL),
                allow_stale=True,
            )
        price = await self._cache.get(instrument, JIN10_WEB_CHANNEL)
        supplement = await self._cache.get(instrument, JIN10_LOCAL_CHANNEL)
        return self._compose_client(
            instrument,
            price,
            supplement,
            allow_stale=True,
        )

    def build_cached(self, instrument: Instrument, source_id: str) -> QuoteView:
        self._require_realtime_source(source_id)
        if source_id == TONGHUASHUN_FUTURES_SOURCE:
            return self._compose_direct(
                instrument,
                source_id,
                self._cache.peek(instrument, source_id),
            )
        if instrument == SPOT_GOLD_CNH_PER_GRAM:
            return self._compose_derived_gold(
                self._cache.peek(SPOT_GOLD, JIN10_WEB_CHANNEL),
                self._cache.peek(USD_CNH, JIN10_WEB_CHANNEL),
            )
        return self._compose_client(
            instrument,
            self._cache.peek(instrument, JIN10_WEB_CHANNEL),
            self._cache.peek(instrument, JIN10_LOCAL_CHANNEL),
        )

    @staticmethod
    def _require_realtime_source(source_id: str) -> None:
        if source_id not in {JIN10_CLIENT_SOURCE, TONGHUASHUN_FUTURES_SOURCE}:
            raise ProviderUnavailableError(f"{source_id} is not a selectable realtime source")

    def _compose_direct(
        self,
        instrument: Instrument,
        source_id: str,
        value: QuoteSnapshot | None,
        *,
        allow_stale: bool = False,
    ) -> QuoteView:
        if value is None:
            raise ProviderUnavailableError(f"{instrument.symbol} 暂时没有实时价格")
        is_stale = self._is_stale(source_id, value)
        if is_stale and not allow_stale:
            raise ProviderUnavailableError(f"{instrument.symbol} 的实时价格已过期")

        optional_fields = (*_SUPPLEMENT_FIELDS, "change", "change_percent")
        unavailable = tuple(
            field_name for field_name in optional_fields if getattr(value, field_name) is None
        )
        stale = (
            tuple(
                field_name
                for field_name in ("last", *optional_fields)
                if getattr(value, field_name) is not None
            )
            if is_stale
            else ()
        )
        quote = RealtimeQuoteSnapshot(
            instrument=instrument,
            last=value.last,
            open=value.open,
            high=value.high,
            low=value.low,
            volume=value.volume,
            change=value.change,
            change_percent=value.change_percent,
            source=SourceMetadata(
                provider=source_id,
                provider_symbol=value.source.provider_symbol,
                observed_at=value.source.observed_at,
                received_at=value.source.received_at,
                raw_payload=value.source.raw_payload,
            ),
        )
        return QuoteView(
            source_id=source_id,
            quote=quote,
            quality=(QuoteQuality.DEGRADED if unavailable or stale else QuoteQuality.COMPLETE),
            unavailable_fields=unavailable,
            stale_fields=stale,
            composed_at=datetime.now(UTC),
        )

    def _compose_client(
        self,
        instrument: Instrument,
        price: QuoteSnapshot | None,
        supplement: QuoteSnapshot | None,
        *,
        allow_stale: bool = False,
    ) -> QuoteView:
        if price is None:
            raise ProviderUnavailableError("金十客户端行情暂时没有实时价格")
        price_is_stale = self._is_stale(JIN10_WEB_CHANNEL, price)
        if price_is_stale and not allow_stale:
            raise ProviderUnavailableError("金十客户端行情的实时价格已过期")

        unavailable_fields = [
            field_name for field_name in _PRICE_FIELDS if getattr(price, field_name) is None
        ]
        stale_fields: list[str] = []
        if price_is_stale:
            stale_fields.extend(
                field_name for field_name in _PRICE_FIELDS if getattr(price, field_name) is not None
            )
        supplement_values: dict[str, Decimal | None] = {
            field_name: None for field_name in _SUPPLEMENT_FIELDS
        }
        if supplement is None:
            unavailable_fields.extend(_SUPPLEMENT_FIELDS)
        elif self._is_stale(JIN10_LOCAL_CHANNEL, supplement):
            stale_fields.extend(_SUPPLEMENT_FIELDS)
            if allow_stale:
                for field_name in _SUPPLEMENT_FIELDS:
                    value = getattr(supplement, field_name)
                    supplement_values[field_name] = value
                    if value is None:
                        unavailable_fields.append(field_name)
        else:
            for field_name in _SUPPLEMENT_FIELDS:
                value = getattr(supplement, field_name)
                supplement_values[field_name] = value
                if value is None:
                    unavailable_fields.append(field_name)

        realtime_quote = RealtimeQuoteSnapshot(
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
            quote=realtime_quote,
            quality=(QuoteQuality.DEGRADED if unavailable or stale else QuoteQuality.COMPLETE),
            unavailable_fields=unavailable,
            stale_fields=stale,
            composed_at=datetime.now(UTC),
        )

    def _compose_derived_gold(
        self,
        gold: QuoteSnapshot | None,
        fx: QuoteSnapshot | None,
        *,
        allow_stale: bool = False,
    ) -> QuoteView:
        if gold is None:
            raise ProviderUnavailableError("人民币金价暂时缺少现货黄金实时价格")
        if fx is None:
            raise ProviderUnavailableError("人民币金价暂时缺少美元兑离岸人民币实时汇率")

        gold_is_stale = self._is_stale(JIN10_WEB_CHANNEL, gold)
        fx_is_stale = self._is_stale(JIN10_WEB_CHANNEL, fx)
        if (gold_is_stale or fx_is_stale) and not allow_stale:
            raise ProviderUnavailableError("人民币金价的一条换算行情已过期")

        last = gold.last * fx.last / TROY_OUNCE_GRAMS
        change: Decimal | None = None
        change_percent: Decimal | None = None
        unavailable_fields = list(_SUPPLEMENT_FIELDS)
        previous_gold = gold.last - gold.change if gold.change is not None else None
        previous_fx = fx.last - fx.change if fx.change is not None else None
        if (
            previous_gold is not None
            and previous_fx is not None
            and previous_gold > 0
            and previous_fx > 0
        ):
            previous = previous_gold * previous_fx / TROY_OUNCE_GRAMS
            change = last - previous
            change_percent = change / previous * Decimal("100")
        else:
            unavailable_fields.extend(("change", "change_percent"))

        stale_fields: list[str] = []
        if gold_is_stale or fx_is_stale:
            stale_fields.extend(("last", "change", "change_percent"))
        received_at = max(gold.source.received_at, fx.source.received_at)
        observed_at = max(gold.source.observed_at, fx.source.observed_at)
        quote = RealtimeQuoteSnapshot(
            instrument=SPOT_GOLD_CNH_PER_GRAM,
            last=last,
            open=None,
            high=None,
            low=None,
            volume=None,
            change=change,
            change_percent=change_percent,
            source=SourceMetadata(
                provider=JIN10_CLIENT_SOURCE,
                provider_symbol="XAUUSD.GOODS*USDCNH.FXCM/31.1034768",
                observed_at=observed_at,
                received_at=received_at,
                raw_payload={
                    "derivation": "XAUUSD * USDCNH / grams_per_troy_ounce",
                    "grams_per_troy_ounce": str(TROY_OUNCE_GRAMS),
                    "gold_observed_at": gold.source.observed_at.isoformat(),
                    "fx_observed_at": fx.source.observed_at.isoformat(),
                },
            ),
        )
        unavailable = tuple(dict.fromkeys(unavailable_fields))
        stale = tuple(dict.fromkeys(stale_fields))
        return QuoteView(
            source_id=JIN10_CLIENT_SOURCE,
            quote=quote,
            quality=(QuoteQuality.DEGRADED if unavailable or stale else QuoteQuality.COMPLETE),
            unavailable_fields=unavailable,
            stale_fields=stale,
            composed_at=datetime.now(UTC),
        )

    def _is_stale(self, source_id: str, quote: QuoteSnapshot) -> bool:
        age = max(0.0, (datetime.now(UTC) - quote.source.received_at).total_seconds())
        return age > self._stale_after(source_id)
