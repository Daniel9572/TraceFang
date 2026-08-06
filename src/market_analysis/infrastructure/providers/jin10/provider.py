from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from market_analysis.domain.errors import (
    ProviderDataError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
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
    SourceMetadata,
)
from market_analysis.infrastructure.mcp import (
    McpError,
    McpToolError,
    StreamableHttpMcpClient,
)
from market_analysis.infrastructure.providers.jin10.symbols import Jin10SymbolMapper
from market_analysis.infrastructure.quota import DailyToolBudget

_BEIJING = ZoneInfo("Asia/Shanghai")


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ProviderDataError(f"jin10 field {field!r} is not decimal-compatible") from error
    if not result.is_finite():
        raise ProviderDataError(f"jin10 field {field!r} is not finite")
    return result


def _datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ProviderDataError(f"jin10 field {field!r} is not a timestamp string")
    normalized = value.replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ProviderDataError(f"jin10 field {field!r} is not ISO-8601") from error
    if result.tzinfo is None or result.utcoffset() is None:
        result = result.replace(tzinfo=_BEIJING)
    return result


def _required_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderDataError(f"jin10 field {field!r} must be an object")
    return value


def _required_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProviderDataError(f"jin10 field {field!r} must be an array")
    return value


class Jin10Provider:
    name = "jin10_mcp"
    _required_tools = frozenset(
        {
            "get_quote",
            "get_kline",
            "list_flash",
            "search_flash",
            "list_news",
            "search_news",
            "get_news",
            "list_calendar",
        }
    )

    def __init__(
        self,
        client: StreamableHttpMcpClient,
        *,
        symbol_mapper: Jin10SymbolMapper | None = None,
        budget: DailyToolBudget | None = None,
    ) -> None:
        self.client = client
        self.symbol_mapper = symbol_mapper or Jin10SymbolMapper()
        self.budget = budget or DailyToolBudget(
            provider=self.name,
            daily_limit=1500,
            reserve=25,
        )
        self._ready = False

    async def __aenter__(self) -> Jin10Provider:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._ready:
            return
        try:
            await self.client.initialize()
            tools_result, resources_result = await asyncio.gather(
                self.client.list_tools(),
                self.client.list_resources(),
            )
        except McpError as error:
            await self.client.close()
            raise ProviderUnavailableError(str(error)) from error
        tools = {
            item.get("name")
            for item in _required_list(tools_result.get("tools"), "tools")
            if isinstance(item, dict)
        }
        missing = self._required_tools - tools
        if missing:
            raise ProviderDataError(f"jin10 is missing tools: {sorted(missing)}")
        resource_uris = {
            item.get("uri")
            for item in _required_list(resources_result.get("resources"), "resources")
            if isinstance(item, dict)
        }
        if "quote://codes" not in resource_uris:
            raise ProviderDataError("jin10 is missing quote://codes resource")
        self._ready = True

    async def close(self) -> None:
        self._ready = False
        await self.client.close()

    def _ensure_ready(self) -> None:
        if not self._ready:
            raise ProviderUnavailableError("jin10 provider is not open")

    async def _tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._ensure_ready()
        await self.budget.acquire(name)
        try:
            result = await self.client.call_tool(name, arguments)
        except McpToolError as error:
            if "调用次数" in error.readable_content or "限流" in error.readable_content:
                raise ProviderRateLimitError(error.readable_content) from error
            raise ProviderDataError(str(error)) from error
        except McpError as error:
            raise ProviderUnavailableError(str(error)) from error
        payload = result.structured_content
        status = payload.get("status")
        message = str(payload.get("message", ""))
        if status != 200:
            if "调用次数" in message or "限流" in message:
                raise ProviderRateLimitError(message)
            raise ProviderDataError(f"jin10 {name} status={status}: {message}")
        return payload

    async def list_instruments(self) -> tuple[InstrumentCatalogEntry, ...]:
        self._ensure_ready()
        try:
            payload = await self.client.read_json_resource("quote://codes")
        except McpError as error:
            raise ProviderUnavailableError(str(error)) from error
        if payload.get("status") != 200:
            raise ProviderDataError(f"jin10 quote catalog status={payload.get('status')}")
        items = _required_list(payload.get("data"), "data")
        entries: list[InstrumentCatalogEntry] = []
        for item in items:
            row = _required_mapping(item, "data[]")
            code = str(row.get("code", ""))
            name = str(row.get("name", ""))
            if code:
                entries.append(
                    InstrumentCatalogEntry(
                        provider=self.name,
                        provider_code=code,
                        name=name,
                        instrument=self.symbol_mapper.known_mapping(code),
                    )
                )
        return tuple(entries)

    async def get_quote(self, instrument: Instrument) -> QuoteSnapshot:
        code = self.symbol_mapper.to_provider_code(instrument)
        payload = await self._tool("get_quote", {"code": code})
        data = _required_mapping(payload.get("data"), "data")
        observed_at = _datetime(data.get("time"), "time")
        return QuoteSnapshot(
            instrument=instrument,
            last=_decimal(data.get("close"), "close"),
            open=_decimal(data.get("open"), "open"),
            high=_decimal(data.get("high"), "high"),
            low=_decimal(data.get("low"), "low"),
            volume=_decimal(data.get("volume"), "volume")
            if data.get("volume") is not None
            else None,
            change=_decimal(data.get("ups_price"), "ups_price")
            if data.get("ups_price") is not None
            else None,
            change_percent=_decimal(data.get("ups_percent"), "ups_percent")
            if data.get("ups_percent") is not None
            else None,
            source=SourceMetadata(
                provider=self.name,
                provider_symbol=code,
                observed_at=observed_at,
                received_at=datetime.now(UTC),
                raw_payload=data,
            ),
        )

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        if not 1 <= count <= 100:
            raise ValueError("count must be between 1 and 100")
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must be timezone-aware")
        code = self.symbol_mapper.to_provider_code(instrument)
        arguments: dict[str, Any] = {"code": code, "count": count}
        if start is not None:
            arguments["time"] = int(start.timestamp())
        payload = await self._tool("get_kline", arguments)
        data = _required_mapping(payload.get("data"), "data")
        rows = _required_list(data.get("klines"), "data.klines")
        received_at = datetime.now(UTC)
        candles: list[Candle] = []
        for row_value in rows:
            row = _required_mapping(row_value, "data.klines[]")
            try:
                observed_at = datetime.fromtimestamp(int(row.get("time")), tz=UTC)
            except (TypeError, ValueError, OSError) as error:
                raise ProviderDataError("jin10 kline time is invalid") from error
            source = SourceMetadata(
                provider=self.name,
                provider_symbol=code,
                observed_at=observed_at,
                received_at=received_at,
                raw_payload=row,
            )
            candles.append(
                Candle(
                    instrument=instrument,
                    interval=timedelta(minutes=1),
                    open_time=observed_at,
                    open=_decimal(row.get("open"), "open"),
                    high=_decimal(row.get("high"), "high"),
                    low=_decimal(row.get("low"), "low"),
                    close=_decimal(row.get("close"), "close"),
                    volume=_decimal(row.get("volume"), "volume")
                    if row.get("volume") is not None
                    else None,
                    source=source,
                )
            )
        candles.sort(key=lambda candle: candle.open_time)
        return tuple(candles)

    async def list_flash(self, cursor: str | None = None) -> FeedPage[FlashItem]:
        arguments = {"cursor": cursor} if cursor else {}
        payload = await self._tool("list_flash", arguments)
        return self._flash_page(payload)

    async def search_flash(self, keyword: str) -> tuple[FlashItem, ...]:
        if not keyword.strip():
            raise ValueError("keyword cannot be empty")
        payload = await self._tool("search_flash", {"keyword": keyword})
        data = _required_mapping(payload.get("data"), "data")
        return tuple(self._flash_items(data.get("items")))

    async def list_news(self, cursor: str | None = None) -> FeedPage[NewsBrief]:
        arguments = {"cursor": cursor} if cursor else {}
        payload = await self._tool("list_news", arguments)
        return self._news_page(payload)

    async def search_news(self, keyword: str, cursor: str | None = None) -> FeedPage[NewsBrief]:
        if not keyword.strip():
            raise ValueError("keyword cannot be empty")
        arguments = {"keyword": keyword}
        if cursor:
            arguments["cursor"] = cursor
        payload = await self._tool("search_news", arguments)
        return self._news_page(payload)

    async def get_news(self, article_id: str) -> NewsArticle:
        if not article_id.strip():
            raise ValueError("article_id cannot be empty")
        payload = await self._tool("get_news", {"id": article_id})
        data = _required_mapping(payload.get("data"), "data")
        return NewsArticle(
            article_id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            introduction=str(data.get("introduction", "")),
            content=str(data.get("content", "")),
            published_at=_datetime(data.get("time"), "time"),
            url=str(data.get("url", "")),
            source_provider=self.name,
        )

    async def list_calendar(self) -> tuple[EconomicEvent, ...]:
        payload = await self._tool("list_calendar", {})
        rows = _required_list(payload.get("data"), "data")
        events: list[EconomicEvent] = []
        for row_value in rows:
            row = _required_mapping(row_value, "data[]")
            events.append(
                EconomicEvent(
                    published_at=_datetime(row.get("pub_time"), "pub_time"),
                    importance=int(row.get("star", 0)),
                    title=str(row.get("title", "")),
                    previous=self._optional_string(row.get("previous")),
                    consensus=self._optional_string(row.get("consensus")),
                    actual=self._optional_string(row.get("actual")),
                    revised=self._optional_string(row.get("revised")),
                    impact_text=str(row.get("affect_txt", "")),
                    source_provider=self.name,
                )
            )
        return tuple(events)

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return None if value is None else str(value)

    def _flash_items(self, value: Any) -> list[FlashItem]:
        items: list[FlashItem] = []
        for item_value in _required_list(value, "data.items"):
            item = _required_mapping(item_value, "data.items[]")
            items.append(
                FlashItem(
                    title=str(item.get("title", "")),
                    content=str(item.get("content", "")),
                    published_at=_datetime(item.get("time"), "time"),
                    url=str(item.get("url", "")),
                    source_provider=self.name,
                )
            )
        return items

    def _flash_page(self, payload: dict[str, Any]) -> FeedPage[FlashItem]:
        data = _required_mapping(payload.get("data"), "data")
        return FeedPage(
            items=tuple(self._flash_items(data.get("items"))),
            next_cursor=str(data.get("next_cursor", "")),
            has_more=bool(data.get("has_more", False)),
        )

    def _news_items(self, value: Any) -> list[NewsBrief]:
        items: list[NewsBrief] = []
        for item_value in _required_list(value, "data.items"):
            item = _required_mapping(item_value, "data.items[]")
            items.append(
                NewsBrief(
                    article_id=str(item.get("id", "")),
                    title=str(item.get("title", "")),
                    introduction=str(item.get("introduction", "")),
                    published_at=_datetime(item.get("time"), "time"),
                    url=str(item.get("url", "")),
                    source_provider=self.name,
                )
            )
        return items

    def _news_page(self, payload: dict[str, Any]) -> FeedPage[NewsBrief]:
        data = _required_mapping(payload.get("data"), "data")
        return FeedPage(
            items=tuple(self._news_items(data.get("items"))),
            next_cursor=str(data.get("next_cursor", "")),
            has_more=bool(data.get("has_more", False)),
        )
