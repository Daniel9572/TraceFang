from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Protocol

from market_analysis.domain.models import Candle, Instrument


class LocalCandleStore(Protocol):
    async def load_candles(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]: ...

    async def save_candles(self, candles: Sequence[Candle]) -> None: ...


CandleFetcher = Callable[
    [Instrument, str, datetime | None, int],
    Awaitable[tuple[Candle, ...]],
]

class LocalCandleHistoryService:
    """Serves persisted history and fills gaps from that same selected source."""

    def __init__(
        self,
        store: LocalCandleStore,
        *,
        fetch_candles: CandleFetcher,
        backfill_delay_seconds: float = 0.05,
        retry_cooldown_seconds: float = 300.0,
    ) -> None:
        self._store = store
        self._fetch_candles = fetch_candles
        self._backfill_delay_seconds = backfill_delay_seconds
        self._retry_cooldown_seconds = retry_cooldown_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cooldown_until: dict[str, float] = {}

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        rows = await self._store.load_candles(
            instrument,
            source_id=source_id,
            start=start,
            count=count,
        )
        if self._needs_backfill(rows, start=start, count=count):
            self._schedule_backfill(
                instrument,
                source_id=source_id,
                start=start,
                count=count,
            )
        return rows

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _needs_backfill(
        rows: Sequence[Candle],
        *,
        start: datetime | None,
        count: int,
    ) -> bool:
        if len(rows) < count:
            return True
        if start is not None or not rows:
            return False
        current_minute = datetime.now(UTC).replace(second=0, microsecond=0)
        return rows[-1].open_time < current_minute

    def _schedule_backfill(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime | None,
        count: int,
    ) -> None:
        interval_key = (
            int(start.timestamp())
            if start is not None
            else int(datetime.now(UTC).replace(second=0, microsecond=0).timestamp())
        )
        key = f"{instrument.symbol}:{source_id}:{interval_key}:{count}"
        if key in self._tasks or self._cooldown_until.get(key, 0.0) > monotonic():
            return
        task = asyncio.create_task(
            self._backfill(
                key,
                instrument,
                source_id=source_id,
                start=start,
                count=count,
            ),
            name=f"candle-backfill-{instrument.symbol}",
        )
        self._tasks[key] = task
        task.add_done_callback(lambda _: self._tasks.pop(key, None))

    async def _backfill(
        self,
        key: str,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime | None,
        count: int,
    ) -> None:
        await asyncio.sleep(self._backfill_delay_seconds)
        try:
            try:
                rows = await self._fetch_candles(instrument, source_id, start, count)
            except Exception:
                rows = ()
            if rows:
                await self._store.save_candles(rows)
        finally:
            self._cooldown_until[key] = monotonic() + self._retry_cooldown_seconds

    def pending_count(self) -> int:
        return len(self._tasks)
