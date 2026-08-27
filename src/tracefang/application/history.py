"""Frozen cross-source history prototype.

The application runtime deliberately does not import this module. It remains only
to preserve the former experiment until the user explicitly redesigns history.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Protocol

from tracefang.domain.models import Candle, Instrument


class LocalCandleStore(Protocol):
    async def load_candles(
        self,
        instrument: Instrument,
        *,
        source_priority: Sequence[str],
        quote_derived_sources: Sequence[str],
        interval: timedelta = timedelta(minutes=1),
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]: ...

    async def save_candles(self, candles: Sequence[Candle]) -> None: ...

    async def standardize_candles(
        self,
        instrument: Instrument,
        *,
        source_priority: Sequence[str],
        quote_derived_sources: Sequence[str],
        start: datetime,
        end: datetime,
        interval: timedelta = timedelta(minutes=1),
    ) -> None: ...


CandleFetcher = Callable[
    [Instrument, str, datetime | None, int],
    Awaitable[tuple[Candle, ...]],
]
SourceList = Callable[[], Sequence[str]]


class LocalCandleHistoryService:
    """Frozen legacy implementation; never wire this cross-source service into runtime."""

    def __init__(
        self,
        store: LocalCandleStore,
        *,
        fetch_candles: CandleFetcher,
        source_priority: SourceList,
        quote_derived_sources: SourceList,
        backfill_sources: SourceList,
        backfill_delay_seconds: float = 0.05,
        retry_cooldown_seconds: float = 300.0,
    ) -> None:
        self._store = store
        self._fetch_candles = fetch_candles
        self._source_priority = source_priority
        self._quote_derived_sources = quote_derived_sources
        self._backfill_sources = backfill_sources
        self._backfill_delay_seconds = backfill_delay_seconds
        self._retry_cooldown_seconds = retry_cooldown_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cooldown_until: dict[str, float] = {}

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]:
        await self._standardize_local_candidates(
            instrument,
            start=start,
            count=count,
        )
        rows = await self._store.load_candles(
            instrument,
            source_priority=self._source_priority(),
            quote_derived_sources=self._quote_derived_sources(),
            start=start,
            count=count,
        )
        self._validate_channel_pure_series(rows, instrument=instrument)
        if self._needs_backfill(rows, start=start, count=count):
            self._schedule_backfill(
                instrument,
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
        start: datetime | None,
        count: int,
    ) -> None:
        interval_key = (
            int(start.timestamp())
            if start is not None
            else int(datetime.now(UTC).replace(second=0, microsecond=0).timestamp())
        )
        key = f"{instrument.symbol}:{interval_key}:{count}"
        if key in self._tasks or self._cooldown_until.get(key, 0.0) > monotonic():
            return
        task = asyncio.create_task(
            self._backfill(
                key,
                instrument,
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
        start: datetime | None,
        count: int,
    ) -> None:
        await asyncio.sleep(self._backfill_delay_seconds)
        try:
            for source_id in self._backfill_sources():
                current = await self._store.load_candles(
                    instrument,
                    source_priority=self._source_priority(),
                    quote_derived_sources=self._quote_derived_sources(),
                    start=start,
                    count=count,
                )
                if not self._needs_backfill(current, start=start, count=count):
                    break
                try:
                    rows = await self._fetch_candles(instrument, source_id, start, count)
                    self._validate_channel_pure_series(
                        rows,
                        instrument=instrument,
                        expected_source=source_id,
                    )
                except Exception:
                    continue
                if rows:
                    await self._store.save_candles(rows)
                    await self._standardize_local_candidates(
                        instrument,
                        start=start,
                        count=count,
                    )
        finally:
            self._cooldown_until[key] = monotonic() + self._retry_cooldown_seconds

    def pending_count(self) -> int:
        return len(self._tasks)

    async def _standardize_local_candidates(
        self,
        instrument: Instrument,
        *,
        start: datetime | None,
        count: int,
    ) -> None:
        current_minute = datetime.now(UTC).replace(second=0, microsecond=0)
        if start is None:
            validation_end = current_minute
            validation_start = validation_end - timedelta(minutes=max(180, count * 3))
        else:
            validation_start = start.replace(second=0, microsecond=0)
            validation_end = min(
                validation_start + timedelta(minutes=count),
                current_minute,
            )
        if validation_end <= validation_start:
            return
        await self._store.standardize_candles(
            instrument,
            source_priority=self._source_priority(),
            quote_derived_sources=self._quote_derived_sources(),
            start=validation_start,
            end=validation_end,
        )

    @staticmethod
    def _validate_channel_pure_series(
        rows: Sequence[Candle],
        *,
        instrument: Instrument,
        expected_source: str | None = None,
    ) -> None:
        sources = {row.source.provider for row in rows}
        if len(sources) > 1:
            raise RuntimeError("local history query returned multiple raw channels in one series")
        if expected_source is not None and sources and sources != {expected_source}:
            raise RuntimeError(
                f"history provider {expected_source} returned rows owned by {sorted(sources)}"
            )
        if sources == {"standard_history"}:
            for row in rows:
                payload = row.source.raw_payload
                if (
                    not isinstance(payload, Mapping)
                    or payload.get("validation_state") != "accepted"
                ):
                    raise RuntimeError(
                        "standard history row is missing accepted validation lineage"
                    )
        seen_times = set()
        previous_time = None
        for row in rows:
            if row.instrument != instrument:
                raise RuntimeError("history provider returned a different instrument")
            if row.open_time in seen_times:
                raise RuntimeError("history provider returned duplicate candle times")
            if previous_time is not None and row.open_time < previous_time:
                raise RuntimeError("history provider returned candles out of order")
            seen_times.add(row.open_time)
            previous_time = row.open_time
