from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from uuid import uuid4

import httpx

from market_analysis.application.provider_frames import ProviderFrame, RawFrameSink
from market_analysis.domain.errors import (
    ProviderDataError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from market_analysis.domain.models import Candle, Instrument, QuoteSnapshot, SourceMetadata
from market_analysis.infrastructure.providers.tonghuashun_futures.protocol import (
    TONGHUASHUN_HTTP_FRAME_ENCODING,
    TONGHUASHUN_HTTP_FRAME_VERSION,
    TONGHUASHUN_TIME_PRECISION,
    TonghuashunDailyStats,
    TonghuashunDecodedDailyFrame,
    TonghuashunDecodedFrame,
    TonghuashunDecodedLineFrame,
    TonghuashunDecodedQuoteFrame,
    TonghuashunHttpFrameKind,
    TonghuashunHttpResponseFrame,
    TonghuashunWireCandle,
    decode_http_response_frame,
    decode_http_response_jsonp,
    encode_http_response_frame,
    parse_daily_stats_payload,
    parse_line_payload,
    parse_time_payload,
    tonghuashun_frame_channel,
)
from market_analysis.infrastructure.providers.tonghuashun_futures.settings import (
    TonghuashunFuturesSettings,
)
from market_analysis.infrastructure.providers.tonghuashun_futures.symbols import (
    TonghuashunFuturesSymbolMapper,
)

QuoteListener = Callable[[QuoteSnapshot], None]


class TonghuashunFuturesProvider:
    """Structured public Tonghuashun quotes and same-source minute lines."""

    name = "tonghuashun_futures"

    def __init__(
        self,
        settings: TonghuashunFuturesSettings,
        *,
        symbol_mapper: TonghuashunFuturesSymbolMapper | None = None,
        http_client: httpx.AsyncClient | None = None,
        frame_sink: RawFrameSink | None = None,
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
        self._frame_sink = frame_sink
        self._frame_connection_id = uuid4().hex
        self._frame_sequence = 0
        self._frame_lock = asyncio.Lock()
        self._daily_cache: dict[str, tuple[float, TonghuashunDailyStats]] = {}
        self._history_cache: dict[tuple[str, int], tuple[float, TonghuashunDecodedLineFrame]] = {}
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
        decoded = await self._get_jsonp(
            self._time_url(provider_code),
            capability="quote",
            kind=TonghuashunHttpFrameKind.TIME,
            provider_code=provider_code,
        )
        if not isinstance(decoded, TonghuashunDecodedQuoteFrame):
            raise ProviderDataError("Tonghuashun quote request decoded as another response kind")
        stats: TonghuashunDailyStats | None = None
        # A current price remains useful if the slower daily-statistics file is
        # briefly unavailable. The quote view exposes missing supplements.
        with suppress(ProviderError):
            stats = await self._daily_stats(
                provider_code,
                trade_date=decoded.quote.trade_date,
            )
        quote_event = self._quote_from_decoded(decoded)
        return self._with_daily_stats(quote_event, stats)

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
        period = self.settings.minute_line_period
        file = "last.js"
        decoded = await self._get_jsonp(
            self._line_url(
                provider_code,
                period=period,
                file=file,
            ),
            capability="Kline",
            kind=TonghuashunHttpFrameKind.MINUTE_LAST,
            provider_code=provider_code,
            period=period,
            file=file,
        )
        if not isinstance(decoded, TonghuashunDecodedLineFrame):
            raise ProviderDataError("Tonghuashun Kline request decoded as another response kind")
        return self._to_candles(
            decoded,
            decoded.rows[-count:],
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
        line_time_zone = self.symbol_mapper.line_time_zone(instrument)
        end = start + timedelta(minutes=count)
        first_year = start.astimezone(line_time_zone).year
        last_year = (end - timedelta(microseconds=1)).astimezone(line_time_zone).year
        batches = await asyncio.gather(
            *(
                self._year_rows(
                    provider_code,
                    year,
                )
                for year in range(first_year, last_year + 1)
            )
        )
        return tuple(
            candle
            for batch in batches
            for candle in self._to_candles(
                batch,
                tuple(row for row in batch.rows if start <= row.open_time < end),
                history_file="tonghuashun_public_line_61_year",
            )
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
            period = self.settings.daily_line_period
            file = "last.js"
            decoded = await self._get_jsonp(
                self._line_url(
                    provider_code,
                    period=period,
                    file=file,
                ),
                capability="daily statistics",
                kind=TonghuashunHttpFrameKind.DAILY_LAST,
                provider_code=provider_code,
                period=period,
                file=file,
                trade_date=trade_date,
            )
            if not isinstance(decoded, TonghuashunDecodedDailyFrame):
                raise ProviderDataError(
                    "Tonghuashun daily-statistics request decoded as another response kind"
                )
            stats = decoded.stats
            if stats is not None:
                self._daily_cache[provider_code] = (monotonic(), stats)
            return stats

    async def _year_rows(
        self,
        provider_code: str,
        year: int,
    ) -> TonghuashunDecodedLineFrame:
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
            period = self.settings.minute_line_period
            file = f"{year}.js"
            decoded = await self._get_jsonp(
                self._line_url(
                    provider_code,
                    period=period,
                    file=file,
                ),
                capability=f"{year} Kline",
                kind=TonghuashunHttpFrameKind.MINUTE_YEAR,
                provider_code=provider_code,
                period=period,
                file=file,
            )
            if not isinstance(decoded, TonghuashunDecodedLineFrame):
                raise ProviderDataError(
                    "Tonghuashun history request decoded as another response kind"
                )
            self._history_cache[key] = (monotonic(), decoded)
            return decoded

    def _to_candles(
        self,
        decoded: TonghuashunDecodedLineFrame,
        rows: tuple[TonghuashunWireCandle, ...],
        *,
        history_file: str,
    ) -> tuple[Candle, ...]:
        response = decoded.response
        instrument = self.symbol_mapper.from_provider_code(response.provider_code)
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
                    provider_symbol=response.provider_code,
                    observed_at=row.open_time,
                    received_at=decoded.received_at,
                    raw_payload={
                        "history_file": history_file,
                        "channel": "tonghuashun_public_line_v6",
                        "frame_channel": tonghuashun_frame_channel(response.kind),
                        "response_kind": response.kind.value,
                        "connection_id": decoded.connection_id,
                        "sequence": decoded.sequence,
                    },
                ),
            )
            for row in rows
        )

    def _quote_from_decoded(
        self,
        decoded: TonghuashunDecodedQuoteFrame,
    ) -> QuoteSnapshot:
        """Normalize the TIME response shared by live delivery and replay."""

        response = decoded.response
        wire = decoded.quote
        instrument = self.symbol_mapper.from_provider_code(response.provider_code)
        change = wire.last - wire.previous_settlement
        change_percent = (change / wire.previous_settlement * Decimal("100")).quantize(
            Decimal("0.01")
        )
        return QuoteSnapshot(
            instrument=instrument,
            last=wire.last,
            open=None,
            high=None,
            low=None,
            volume=None,
            change=change,
            change_percent=change_percent,
            source=SourceMetadata(
                provider=self.name,
                provider_symbol=response.provider_code,
                # The public TIME payload exposes only HHMM. A captured frame's
                # arrival timestamp is therefore the only deterministic 1S Bar
                # clock shared by live delivery and replay.
                observed_at=decoded.received_at,
                received_at=decoded.received_at,
                raw_payload={
                    "channel": "tonghuashun_public_time_v6",
                    "frame_channel": tonghuashun_frame_channel(response.kind),
                    "response_kind": response.kind.value,
                    "name": wire.name,
                    "trade_date": wire.trade_date,
                    "wire_observed_at": wire.observed_at.isoformat(),
                    "wire_time_precision": TONGHUASHUN_TIME_PRECISION,
                    "bar_clock": "provider_frame.received_at",
                    "price_digits": self.symbol_mapper.price_digits(instrument),
                    "previous_settlement": str(wire.previous_settlement),
                    "daily_stats_available": False,
                    "connection_id": decoded.connection_id,
                    "sequence": decoded.sequence,
                },
            ),
        )

    @staticmethod
    def _with_daily_stats(
        quote: QuoteSnapshot,
        stats: TonghuashunDailyStats | None,
    ) -> QuoteSnapshot:
        """Build the enriched query view without changing the realtime event."""

        if stats is None:
            return quote
        raw_payload = dict(quote.source.raw_payload or {})
        raw_payload["daily_stats_available"] = True
        return QuoteSnapshot(
            instrument=quote.instrument,
            last=quote.last,
            open=stats.open,
            high=stats.high,
            low=stats.low,
            volume=stats.volume,
            change=quote.change,
            change_percent=quote.change_percent,
            source=SourceMetadata(
                provider=quote.source.provider,
                provider_symbol=quote.source.provider_symbol,
                observed_at=quote.source.observed_at,
                received_at=quote.source.received_at,
                raw_payload=raw_payload,
            ),
        )

    def decode_frame(self, frame: ProviderFrame) -> TonghuashunDecodedFrame:
        """Pure decode path shared by live HTTP requests and replay sessions."""

        if frame.version != TONGHUASHUN_HTTP_FRAME_VERSION:
            raise ProviderDataError("unsupported Tonghuashun provider frame version")
        if frame.encoding != TONGHUASHUN_HTTP_FRAME_ENCODING:
            raise ProviderDataError("frame does not use the Tonghuashun HTTP recording encoding")
        response = decode_http_response_frame(frame.body)
        expected_channel = tonghuashun_frame_channel(response.kind)
        if frame.channel != expected_channel:
            raise ProviderDataError("Tonghuashun frame channel does not match its response kind")
        self._raise_http_error(response)
        payload = decode_http_response_jsonp(response)
        instrument = self.symbol_mapper.from_provider_code(response.provider_code)
        expected_name = self.symbol_mapper.expected_name(instrument)
        if response.kind is TonghuashunHttpFrameKind.TIME:
            return TonghuashunDecodedQuoteFrame(
                response=response,
                connection_id=frame.connection_id,
                sequence=frame.sequence,
                received_at=frame.received_at,
                quote=parse_time_payload(
                    payload,
                    expected_provider_code=response.provider_code,
                    expected_name=expected_name,
                    calendar_mode=self.symbol_mapper.quote_calendar_mode(instrument),
                ),
            )
        if response.kind is TonghuashunHttpFrameKind.DAILY_LAST:
            trade_date = response.trade_date
            if trade_date is None:
                raise ProviderDataError("Tonghuashun daily frame is missing its trade date")
            return TonghuashunDecodedDailyFrame(
                response=response,
                connection_id=frame.connection_id,
                sequence=frame.sequence,
                received_at=frame.received_at,
                stats=parse_daily_stats_payload(
                    payload,
                    expected_name=expected_name,
                    expected_trade_date=trade_date,
                ),
            )
        return TonghuashunDecodedLineFrame(
            response=response,
            connection_id=frame.connection_id,
            sequence=frame.sequence,
            received_at=frame.received_at,
            rows=parse_line_payload(
                payload,
                expected_name=expected_name,
                time_zone=self.symbol_mapper.line_time_zone(instrument),
            ),
        )

    async def ingest_frame(
        self,
        frame: ProviderFrame,
        *,
        on_quote: QuoteListener | None = None,
    ) -> QuoteSnapshot | None:
        """Replay a live response without capture or mutation of provider caches."""

        decoded = self.decode_frame(frame)
        if not isinstance(decoded, TonghuashunDecodedQuoteFrame):
            # Daily statistics are quote supplements and minute responses are
            # finite history snapshots. Neither is a realtime market event.
            return None
        quote = self._quote_from_decoded(decoded)
        if on_quote is not None:
            on_quote(quote)
        return quote

    def _time_url(self, provider_code: str) -> str:
        return self.settings.time_endpoint_template.format(provider_code=provider_code)

    def _line_url(self, provider_code: str, *, period: str, file: str) -> str:
        return self.settings.line_endpoint_template.format(
            provider_code=provider_code,
            period=period,
            file=file,
        )

    async def _get_jsonp(
        self,
        endpoint: str,
        *,
        capability: str,
        kind: TonghuashunHttpFrameKind,
        provider_code: str,
        period: str | None = None,
        file: str | None = None,
        trade_date: str | None = None,
    ) -> TonghuashunDecodedFrame:
        try:
            response = await self._http.get(endpoint)
        except httpx.HTTPError as error:
            raise ProviderUnavailableError(f"同花顺公开行情{capability}请求失败") from error
        received_at = datetime.now(UTC)
        recorded_response = TonghuashunHttpResponseFrame(
            version=TONGHUASHUN_HTTP_FRAME_VERSION,
            kind=kind,
            provider_code=provider_code,
            capability=capability,
            request_url=str(response.request.url),
            status_code=response.status_code,
            content_type=response.headers.get("content-type"),
            text_encoding=response.encoding or "utf-8",
            content=response.content,
            period=period,
            file=file,
            trade_date=trade_date,
        )
        frame = await self._capture_response(recorded_response, received_at=received_at)
        return self.decode_frame(frame)

    async def _capture_response(
        self,
        response: TonghuashunHttpResponseFrame,
        *,
        received_at: datetime,
    ) -> ProviderFrame:
        async with self._frame_lock:
            self._frame_sequence += 1
            frame = ProviderFrame(
                version=TONGHUASHUN_HTTP_FRAME_VERSION,
                channel=tonghuashun_frame_channel(response.kind),
                connection_id=self._frame_connection_id,
                sequence=self._frame_sequence,
                received_at=received_at,
                encoding=TONGHUASHUN_HTTP_FRAME_ENCODING,
                body=encode_http_response_frame(response),
            )
            if self._frame_sink is not None:
                await self._frame_sink.capture(frame)
            return frame

    @staticmethod
    def _raise_http_error(response: TonghuashunHttpResponseFrame) -> None:
        if response.status_code == 429:
            raise ProviderRateLimitError("同花顺公开行情接口暂时限流")
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"同花顺公开行情{response.capability}接口返回 HTTP {response.status_code}"
            )
