from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any

import httpx

from market_analysis.domain.errors import (
    ProviderDataError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from market_analysis.domain.models import Candle, Instrument, QuoteSnapshot, SourceMetadata
from market_analysis.infrastructure.providers.tonghuashun_futures.protocol import (
    TonghuashunDailyStats,
    TonghuashunWireCandle,
    decode_jsonp,
    parse_daily_stats_payload,
    parse_line_payload,
    parse_time_payload,
)
from market_analysis.infrastructure.providers.tonghuashun_futures.settings import (
    TonghuashunFuturesSettings,
)
from market_analysis.infrastructure.providers.tonghuashun_futures.symbols import (
    TonghuashunFuturesSymbolMapper,
)


class TonghuashunFuturesProvider:
    """Structured public Tonghuashun quotes and same-source minute lines."""

    name = "tonghuashun_futures"

    def __init__(
        self,
        settings: TonghuashunFuturesSettings,
        *,
        symbol_mapper: TonghuashunFuturesSymbolMapper | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.symbol_mapper = symbol_mapper or TonghuashunFuturesSymbolMapper()
        self._http = http_client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers={
                "Accept": "application/javascript,text/javascript,*/*;q=0.8",
                "Referer": "https://goodsfu.10jqka.com.cn/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/138.0.0.0 Safari/537.36"
                ),
            },
        )
        self._owns_http_client = http_client is None
        self._daily_cache: dict[str, tuple[float, TonghuashunDailyStats]] = {}
        self._history_cache: dict[
            tuple[str, int], tuple[float, tuple[TonghuashunWireCandle, ...]]
        ] = {}
        self._daily_locks: dict[str, asyncio.Lock] = {}
        self._history_locks: dict[tuple[str, int], asyncio.Lock] = {}

    async def __aenter__(self) -> TonghuashunFuturesProvider:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        self._daily_cache.clear()
        self._history_cache.clear()
        if self._owns_http_client:
            await self._http.aclose()

    def provider_symbol(self, instrument: Instrument) -> str:
        return self.symbol_mapper.to_provider_code(instrument)

    async def get_quote(self, instrument: Instrument) -> QuoteSnapshot:
        provider_code = self.provider_symbol(instrument)
        expected_name = self.symbol_mapper.expected_name(instrument)
        payload = await self._get_jsonp(
            self._time_url(provider_code),
            capability="quote",
        )
        wire = parse_time_payload(
            payload,
            expected_provider_code=provider_code,
            expected_name=expected_name,
            calendar_mode=self.symbol_mapper.quote_calendar_mode(instrument),
        )
        stats: TonghuashunDailyStats | None = None
        # A current price remains useful if the slower daily-statistics file is
        # briefly unavailable. The quote view exposes missing supplements.
        with suppress(ProviderError):
            stats = await self._daily_stats(
                provider_code,
                expected_name=expected_name,
                trade_date=wire.trade_date,
            )
        received_at = datetime.now(UTC)
        change = wire.last - wire.previous_settlement
        change_percent = (
            change / wire.previous_settlement * Decimal("100")
        ).quantize(Decimal("0.01"))
        return QuoteSnapshot(
            instrument=instrument,
            last=wire.last,
            open=stats.open if stats is not None else None,
            high=stats.high if stats is not None else None,
            low=stats.low if stats is not None else None,
            volume=stats.volume if stats is not None else None,
            change=change,
            change_percent=change_percent,
            source=SourceMetadata(
                provider=self.name,
                provider_symbol=provider_code,
                observed_at=wire.observed_at,
                received_at=received_at,
                raw_payload={
                    "channel": "tonghuashun_public_time_v6",
                    "name": wire.name,
                    "trade_date": wire.trade_date,
                    "price_digits": self.symbol_mapper.price_digits(instrument),
                    "previous_settlement": str(wire.previous_settlement),
                    "daily_stats_available": stats is not None,
                },
            ),
        )

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        self._validate_window(start, count)
        if start is not None:
            return await self.fetch_historical_candles(
                instrument,
                start=start,
                count=count,
            )
        provider_code = self.provider_symbol(instrument)
        payload = await self._get_jsonp(
            self._line_url(
                provider_code,
                period=self.settings.minute_line_period,
                file="last.js",
            ),
            capability="Kline",
        )
        rows = parse_line_payload(
            payload,
            expected_name=self.symbol_mapper.expected_name(instrument),
            time_zone=self.symbol_mapper.line_time_zone(instrument),
        )
        return self._to_candles(
            instrument,
            provider_code,
            rows[-count:],
            history_file="tonghuashun_public_line_61_last",
        )

    async def fetch_historical_candles(
        self,
        instrument: Instrument,
        *,
        start: datetime,
        count: int,
    ) -> tuple[Candle, ...]:
        self._validate_window(start, count)
        provider_code = self.provider_symbol(instrument)
        expected_name = self.symbol_mapper.expected_name(instrument)
        line_time_zone = self.symbol_mapper.line_time_zone(instrument)
        end = start + timedelta(minutes=count)
        first_year = start.astimezone(line_time_zone).year
        last_year = (end - timedelta(microseconds=1)).astimezone(line_time_zone).year
        batches = await asyncio.gather(
            *(
                self._year_rows(
                    provider_code,
                    year,
                    expected_name=expected_name,
                    time_zone=line_time_zone,
                )
                for year in range(first_year, last_year + 1)
            )
        )
        rows = tuple(
            row
            for batch in batches
            for row in batch
            if start <= row.open_time < end
        )
        return self._to_candles(
            instrument,
            provider_code,
            rows,
            history_file="tonghuashun_public_line_61_year",
        )

    @staticmethod
    def _validate_window(start: datetime | None, count: int) -> None:
        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must be timezone-aware")

    async def _daily_stats(
        self,
        provider_code: str,
        *,
        expected_name: str,
        trade_date: str,
    ) -> TonghuashunDailyStats | None:
        cached = self._daily_cache.get(provider_code)
        now = monotonic()
        if (
            cached is not None
            and cached[1].trade_date == trade_date
            and now - cached[0] < self.settings.daily_stats_cache_seconds
        ):
            return cached[1]
        lock = self._daily_locks.setdefault(provider_code, asyncio.Lock())
        async with lock:
            cached = self._daily_cache.get(provider_code)
            now = monotonic()
            if (
                cached is not None
                and cached[1].trade_date == trade_date
                and now - cached[0] < self.settings.daily_stats_cache_seconds
            ):
                return cached[1]
            payload = await self._get_jsonp(
                self._line_url(
                    provider_code,
                    period=self.settings.daily_line_period,
                    file="last.js",
                ),
                capability="daily statistics",
            )
            stats = parse_daily_stats_payload(
                payload,
                expected_name=expected_name,
                expected_trade_date=trade_date,
            )
            if stats is not None:
                self._daily_cache[provider_code] = (monotonic(), stats)
            return stats

    async def _year_rows(
        self,
        provider_code: str,
        year: int,
        *,
        expected_name: str,
        time_zone: Any,
    ) -> tuple[TonghuashunWireCandle, ...]:
        key = (provider_code, year)
        cached = self._history_cache.get(key)
        now = monotonic()
        if cached is not None and now - cached[0] < self.settings.history_cache_seconds:
            return cached[1]
        lock = self._history_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._history_cache.get(key)
            now = monotonic()
            if cached is not None and now - cached[0] < self.settings.history_cache_seconds:
                return cached[1]
            payload = await self._get_jsonp(
                self._line_url(
                    provider_code,
                    period=self.settings.minute_line_period,
                    file=f"{year}.js",
                ),
                capability=f"{year} Kline",
            )
            rows = parse_line_payload(
                payload,
                expected_name=expected_name,
                time_zone=time_zone,
            )
            self._history_cache[key] = (monotonic(), rows)
            return rows

    def _to_candles(
        self,
        instrument: Instrument,
        provider_code: str,
        rows: tuple[TonghuashunWireCandle, ...],
        *,
        history_file: str,
    ) -> tuple[Candle, ...]:
        received_at = datetime.now(UTC)
        return tuple(
            Candle(
                instrument=instrument,
                interval=timedelta(minutes=1),
                open_time=row.open_time,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                source=SourceMetadata(
                    provider=self.name,
                    provider_symbol=provider_code,
                    observed_at=row.open_time,
                    received_at=received_at,
                    raw_payload={
                        "history_file": history_file,
                        "channel": "tonghuashun_public_line_v6",
                    },
                ),
            )
            for row in rows
        )

    def _time_url(self, provider_code: str) -> str:
        return self.settings.time_endpoint_template.format(provider_code=provider_code)

    def _line_url(self, provider_code: str, *, period: str, file: str) -> str:
        return self.settings.line_endpoint_template.format(
            provider_code=provider_code,
            period=period,
            file=file,
        )

    async def _get_jsonp(self, endpoint: str, *, capability: str) -> Mapping[str, Any]:
        try:
            response = await self._http.get(endpoint)
        except httpx.HTTPError as error:
            raise ProviderUnavailableError(
                f"同花顺公开行情{capability}请求失败"
            ) from error
        if response.status_code == 429:
            raise ProviderRateLimitError("同花顺公开行情接口暂时限流")
        if response.is_error:
            raise ProviderUnavailableError(
                f"同花顺公开行情{capability}接口返回 HTTP {response.status_code}"
            )
        try:
            return decode_jsonp(response.text)
        except ProviderDataError:
            raise
        except ValueError as error:
            raise ProviderDataError(
                f"同花顺公开行情{capability}接口返回了无效数据"
            ) from error
