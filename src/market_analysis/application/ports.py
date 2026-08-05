from __future__ import annotations

from datetime import datetime
from typing import Protocol

from market_analysis.domain.models import (
    Candle,
    EconomicEvent,
    FeedPage,
    FlashItem,
    Instrument,
    InstrumentCatalogEntry,
    NewsArticle,
    NewsBrief,
    QuoteSnapshot,
)


class QuoteProvider(Protocol):
    name: str

    async def get_quote(self, instrument: Instrument) -> QuoteSnapshot: ...


class CandleProvider(Protocol):
    name: str

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]: ...


class InstrumentCatalogProvider(Protocol):
    name: str

    async def list_instruments(self) -> tuple[InstrumentCatalogEntry, ...]: ...


class NewsProvider(Protocol):
    name: str

    async def list_flash(self, cursor: str | None = None) -> FeedPage[FlashItem]: ...

    async def search_flash(self, keyword: str) -> tuple[FlashItem, ...]: ...

    async def list_news(self, cursor: str | None = None) -> FeedPage[NewsBrief]: ...

    async def search_news(self, keyword: str, cursor: str | None = None) -> FeedPage[NewsBrief]: ...

    async def get_news(self, article_id: str) -> NewsArticle: ...


class CalendarProvider(Protocol):
    name: str

    async def list_calendar(self) -> tuple[EconomicEvent, ...]: ...
