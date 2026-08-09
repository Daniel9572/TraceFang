from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from market_analysis.application.persistence import MarketDataStore, PersistenceHealth
from market_analysis.domain.market_events import RealtimeBar
from market_analysis.domain.models import Candle, QuoteSnapshot


@dataclass(frozen=True, slots=True)
class _WriteQuote:
    value: QuoteSnapshot


@dataclass(frozen=True, slots=True)
class _WriteCandles:
    values: tuple[Candle, ...]


@dataclass(frozen=True, slots=True)
class _WriteRealtimeBars:
    values: tuple[RealtimeBar, ...]


_WriteRequest = _WriteQuote | _WriteCandles | _WriteRealtimeBars


class BufferedMarketDataWriter:
    """Keeps database I/O off the quote delivery path and retries without source fallback."""

    def __init__(
        self,
        store: MarketDataStore,
        *,
        queue_size: int = 10_000,
        reconnect_seconds: float = 5.0,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        if reconnect_seconds <= 0:
            raise ValueError("reconnect_seconds must be positive")
        self._store = store
        self._queue: asyncio.Queue[_WriteRequest] = asyncio.Queue(maxsize=queue_size)
        self._reconnect_seconds = reconnect_seconds
        self._task: asyncio.Task[None] | None = None
        self._state = "starting"
        self._detail: str | None = None
        self._last_write_at: datetime | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="market-data-postgres-writer")
        await asyncio.sleep(0)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._store.close()
        self._state = "stopped"

    def submit_quote(self, quote: QuoteSnapshot) -> bool:
        return self._submit(_WriteQuote(quote))

    def submit_candles(self, candles: Sequence[Candle]) -> bool:
        rows = tuple(candles)
        if not rows:
            return True
        return self._submit(_WriteCandles(rows))

    def submit_realtime_bars(self, bars: Sequence[RealtimeBar]) -> bool:
        rows = tuple(bars)
        if not rows:
            return True
        return self._submit(_WriteRealtimeBars(rows))

    def _submit(self, request: _WriteRequest) -> bool:
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            self._state = "backpressure"
            self._detail = "PostgreSQL write queue is full; ingestion has been paused"
            return False
        return True

    def health(self) -> PersistenceHealth:
        return PersistenceHealth(
            state=self._state,
            detail=self._detail,
            queue_depth=self._queue.qsize(),
            last_write_at=self._last_write_at,
        )

    async def _run(self) -> None:
        pending: _WriteRequest | None = None
        while True:
            try:
                await self._store.open()
            except asyncio.CancelledError:
                raise
            # Connection errors are reflected through health, then retried.
            except Exception as error:
                self._state = "unavailable"
                self._detail = self._safe_error(error)
                await asyncio.sleep(self._reconnect_seconds)
                continue

            self._state = "healthy"
            self._detail = None
            if pending is None:
                pending = await self._queue.get()
            try:
                if isinstance(pending, _WriteQuote):
                    await self._store.save_quote(pending.value)
                elif isinstance(pending, _WriteCandles):
                    await self._store.save_candles(pending.values)
                else:
                    await self._store.save_realtime_bars(pending.values)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._state = "unavailable"
                self._detail = self._safe_error(error)
                await self._store.close()
                await asyncio.sleep(self._reconnect_seconds)
                continue

            pending = None
            self._last_write_at = datetime.now(UTC)
            self._queue.task_done()

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error).replace("\r", " ").replace("\n", " ").strip()
        return message[:240] or type(error).__name__
