"""Deprecated compatibility surface; runtime code lives in ``realtime_bars``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from tracefang.application.quotes import QuoteView
from tracefang.application.realtime_bars import BarBackfillResult as _BarBackfillResult
from tracefang.application.realtime_bars import HistoricalBarBatch
from tracefang.application.realtime_bars import RealtimeBarContract as _RealtimeBarContract
from tracefang.application.realtime_bars import RealtimeBarService as _RealtimeBarService
from tracefang.domain.errors import ProviderUnavailableError
from tracefang.domain.models import Candle, Instrument, QuoteSnapshot, SourceMetadata


class SourceCandleStore(Protocol):
    async def load_source_candles(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]: ...

    async def load_quote_candles(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]: ...

    async def save_candles(self, candles: tuple[Candle, ...]) -> None: ...

    async def candle_missing_ranges(
        self,
        instrument: Instrument,
        *,
        realtime_source_id: str,
        start: datetime,
        end: datetime,
        interval: timedelta = timedelta(minutes=1),
    ) -> tuple[tuple[datetime, datetime], ...]: ...

    async def record_candle_cache_range(
        self,
        instrument: Instrument,
        *,
        realtime_source_id: str,
        upstream_channel_id: str,
        provider_symbol: str,
        start: datetime,
        end: datetime,
        row_count: int,
        interval: timedelta = timedelta(minutes=1),
    ) -> None: ...


class HistoricalCandleProvider(Protocol):
    name: str

    def provider_symbol(self, instrument: Instrument) -> str: ...

    async def fetch_historical_candles(
        self,
        instrument: Instrument,
        *,
        start: datetime,
        count: int,
    ) -> HistoricalBarBatch: ...


@dataclass(frozen=True, slots=True)
class RealtimeKlineBinding:
    """Declares the private Kline channels owned by one complete realtime source."""

    realtime_source_id: str
    history_channel_id: str
    live_quote_channel_id: str
    history_provider: HistoricalCandleProvider | None = None


@dataclass(frozen=True, slots=True)
class CandleBackfillResult:
    source_id: str
    state: str
    start: datetime
    end: datetime
    row_count: int


class RealtimeKlineService:
    """Serves and backfills Klines inside one realtime source's private namespace."""

    def __init__(
        self,
        store: SourceCandleStore | None,
        *,
        bindings: tuple[RealtimeKlineBinding, ...],
    ) -> None:
        if not bindings:
            raise ValueError("at least one realtime Kline binding is required")
        self._store = store
        self._bindings = {item.realtime_source_id: item for item in bindings}
        if len(self._bindings) != len(bindings):
            raise ValueError("realtime Kline source ids must be unique")
        self._source_by_live_channel: dict[str, str] = {}
        for binding in bindings:
            channel_id = binding.live_quote_channel_id
            if channel_id in self._source_by_live_channel:
                raise ValueError(
                    f"live Kline channel {channel_id!r} belongs to multiple realtime sources"
                )
            self._source_by_live_channel[channel_id] = binding.realtime_source_id
        self._live: dict[tuple[str, Instrument, datetime], Candle] = {}
        self._backfills: dict[
            tuple[str, Instrument, datetime, int],
            asyncio.Task[CandleBackfillResult],
        ] = {}
        self._backfill_locks: dict[tuple[str, Instrument], asyncio.Lock] = {}

    def accept_quote(self, quote: QuoteSnapshot) -> bool:
        """Updates only the Kline namespace that owns this exact quote channel."""

        realtime_source_id = self._source_by_live_channel.get(quote.source.provider)
        if realtime_source_id is None:
            return False
        return self._accept_value(
            realtime_source_id,
            quote.instrument,
            quote.last,
            quote.source,
        )

    def accept_view(self, view: QuoteView) -> bool:
        """Builds live-only minute bars for an application-derived realtime view."""

        if view.source_id not in self._bindings:
            return False
        return self._accept_value(
            view.source_id,
            view.quote.instrument,
            view.quote.last,
            view.quote.source,
        )

    def _accept_value(
        self,
        realtime_source_id: str,
        instrument: Instrument,
        last: Decimal,
        metadata: SourceMetadata,
    ) -> bool:
        open_time = metadata.observed_at.replace(second=0, microsecond=0)
        key = (realtime_source_id, instrument, open_time)
        current = self._live.get(key)
        if current is not None and metadata.received_at < current.source.received_at:
            return False
        if current is None:
            value = Candle(
                instrument=instrument,
                interval=timedelta(minutes=1),
                open_time=open_time,
                open=last,
                high=last,
                low=last,
                close=last,
                volume=None,
                source=self._public_metadata(realtime_source_id, metadata),
            )
        else:
            value = Candle(
                instrument=current.instrument,
                interval=current.interval,
                open_time=current.open_time,
                open=current.open,
                high=max(current.high, last),
                low=min(current.low, last),
                close=last,
                volume=None,
                source=self._public_metadata(realtime_source_id, metadata),
            )
        self._live[key] = value
        self._trim_live(realtime_source_id, instrument)
        return True

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        """Reads local rows only. It never invokes an upstream provider."""

        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must be timezone-aware")
        binding = self._bindings.get(source_id)
        if binding is None:
            raise ProviderUnavailableError(f"{source_id} has no source-bound Kline capability")

        historical: tuple[Candle, ...] = ()
        quote_derived: tuple[Candle, ...] = ()
        if self._store is not None:
            historical = await self._store.load_source_candles(
                instrument,
                source_id=binding.history_channel_id,
                start=start,
                count=count,
            )
            quote_derived = await self._store.load_quote_candles(
                instrument,
                source_id=binding.live_quote_channel_id,
                start=start,
                count=count,
            )

        rows: dict[datetime, Candle] = {}
        for candle in historical:
            if (
                candle.instrument != instrument
                or candle.source.provider != binding.history_channel_id
            ):
                raise RuntimeError("source-bound Kline cache returned foreign evidence")
            rows[candle.open_time] = self._as_realtime_source(candle, source_id)

        for candle in quote_derived:
            if (
                candle.instrument != instrument
                or candle.source.provider != binding.live_quote_channel_id
            ):
                raise RuntimeError("source-bound quote cache returned foreign evidence")
            current = rows.get(candle.open_time)
            rows[candle.open_time] = self._merge_same_minute(
                current,
                self._as_realtime_source(candle, source_id),
            )

        end = start + timedelta(minutes=count) if start is not None else None
        for (candidate_source, candidate_instrument, open_time), candle in self._live.items():
            if candidate_source != source_id or candidate_instrument != instrument:
                continue
            if start is not None and open_time < start:
                continue
            if end is not None and open_time >= end:
                continue
            rows[open_time] = self._merge_same_minute(rows.get(open_time), candle)

        ordered = sorted(rows.values(), key=lambda item: item.open_time)
        return tuple(ordered[:count] if start is not None else ordered[-count:])

    async def backfill(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime,
        count: int,
    ) -> CandleBackfillResult:
        """Coalesces one same-source fetch and persists both rows and range coverage."""

        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("start must be timezone-aware")
        if not 1 <= count <= 10_000:
            raise ValueError("count must be between 1 and 10000")
        if source_id not in self._bindings:
            raise ProviderUnavailableError(f"{source_id} has no source-bound Kline capability")
        key = (source_id, instrument, start, count)
        task = self._backfills.get(key)
        if task is None:
            task = asyncio.create_task(
                self._backfill_once(
                    instrument,
                    source_id=source_id,
                    start=start,
                    count=count,
                ),
                name=f"kline-backfill:{source_id}:{instrument.symbol}",
            )
            self._backfills[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._backfills.get(key) is task:
                del self._backfills[key]

    async def _backfill_once(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime,
        count: int,
    ) -> CandleBackfillResult:
        binding = self._bindings[source_id]
        store = self._store
        provider = binding.history_provider
        if store is None:
            raise ProviderUnavailableError("本地历史存储不可用, 不能执行同源回补")
        if provider is None:
            raise ProviderUnavailableError(f"{source_id} 没有可用的同源历史 K 线通道")
        end = start + timedelta(minutes=count)
        lock = self._backfill_locks.setdefault((source_id, instrument), asyncio.Lock())
        async with lock:
            missing = await store.candle_missing_ranges(
                instrument,
                realtime_source_id=source_id,
                start=start,
                end=end,
            )
            if not missing:
                return CandleBackfillResult(source_id, "cached", start, end, 0)

            row_count = 0
            for missing_start, missing_end in missing:
                missing_count = int((missing_end - missing_start) / timedelta(minutes=1))
                batch = await provider.fetch_historical_candles(
                    instrument,
                    start=missing_start,
                    count=missing_count,
                )
                values = batch.candles
                for candle in values:
                    if (
                        candle.instrument != instrument
                        or candle.source.provider != binding.history_channel_id
                        or candle.interval != timedelta(minutes=1)
                        or not missing_start <= candle.open_time < missing_end
                    ):
                        raise RuntimeError("same-source Kline provider returned foreign evidence")
                await store.save_candles(values)
                await store.record_candle_cache_range(
                    instrument,
                    realtime_source_id=source_id,
                    upstream_channel_id=binding.history_channel_id,
                    provider_symbol=provider.provider_symbol(instrument),
                    start=missing_start,
                    end=missing_end,
                    row_count=len(values),
                )
                row_count += len(values)
            return CandleBackfillResult(source_id, "fetched", start, end, row_count)

    def live_count(self) -> int:
        return len(self._live)

    def pending_backfill_count(self) -> int:
        return len(self._backfills)

    async def close(self) -> None:
        tasks = tuple(self._backfills.values())
        self._backfills.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _merge_same_minute(stored: Candle | None, live: Candle) -> Candle:
        if stored is None:
            return live
        return Candle(
            instrument=stored.instrument,
            interval=stored.interval,
            open_time=stored.open_time,
            open=stored.open,
            high=max(stored.high, live.high),
            low=min(stored.low, live.low),
            close=live.close,
            volume=stored.volume,
            source=live.source,
        )

    @staticmethod
    def _as_realtime_source(candle: Candle, source_id: str) -> Candle:
        return Candle(
            instrument=candle.instrument,
            interval=candle.interval,
            open_time=candle.open_time,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            source=RealtimeKlineService._public_metadata(source_id, candle.source),
        )

    @staticmethod
    def _public_metadata(source_id: str, metadata: SourceMetadata) -> SourceMetadata:
        is_history = bool(metadata.raw_payload and metadata.raw_payload.get("history_file"))
        return SourceMetadata(
            provider=source_id,
            provider_symbol=metadata.provider_symbol,
            observed_at=metadata.observed_at,
            received_at=metadata.received_at,
            raw_payload={
                "cache_scope": "realtime_source",
                "derivation": ("same_source_history" if is_history else "same_source_quote_events"),
            },
        )

    def _trim_live(self, source_id: str, instrument: Instrument) -> None:
        keys = sorted(
            (key for key in self._live if key[0] == source_id and key[1] == instrument),
            key=lambda item: item[2],
        )
        for key in keys[:-240]:
            del self._live[key]


# Import compatibility only. New application code must use realtime_bars directly.
CandleBackfillResult = _BarBackfillResult  # noqa: F811
RealtimeKlineBinding = _RealtimeBarContract  # noqa: F811
RealtimeKlineService = _RealtimeBarService  # noqa: F811
