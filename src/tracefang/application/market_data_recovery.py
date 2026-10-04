from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from tracefang.application.realtime_bars import (
    BarBackfillResult,
    BarBackfillState,
    RealtimeBarSeriesState,
)
from tracefang.domain.market_events import RealtimeBar
from tracefang.domain.models import Instrument


class RecoveryBarService(Protocol):
    async def get_bars_before(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta | None = None,
        before: datetime | None = None,
        count: int = 2_000,
    ) -> tuple[RealtimeBar, ...]: ...

    async def backfill(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime,
        count: int,
        revalidate: bool = False,
    ) -> BarBackfillResult: ...

    def history_backfill_configured(self, source_id: str) -> bool: ...

    def series_state(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta | None = None,
    ) -> RealtimeBarSeriesState | None: ...


@dataclass(frozen=True, slots=True)
class MarketDataRecoveryMetrics:
    pending_ranges: int
    active_series: int
    completed_ranges: int
    recovered_rows: int
    failures: int
    last_failure_at: datetime | None
    last_failure_type: str | None


@dataclass(slots=True)
class _RecoveryTarget:
    instrument: Instrument
    source_id: str
    schedule: Mapping[str, Any] | None
    last_observed_open_time: datetime | None = None
    pending: list[tuple[datetime, datetime]] = field(default_factory=list)
    worker: asyncio.Task[None] | None = None
    audit: asyncio.Task[None] | None = None


def _clock(value: object) -> time:
    if not isinstance(value, str):
        raise ValueError("market schedule clock must be a string")
    hour_text, minute_text = value.split(":", maxsplit=1)
    return time(hour=int(hour_text), minute=int(minute_text))


def market_open_ranges(
    start: datetime,
    end: datetime,
    schedule: Mapping[str, Any] | None,
) -> tuple[tuple[datetime, datetime], ...]:
    """Intersect an aligned UTC window with the configured trading sessions."""

    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("recovery start must be timezone-aware")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("recovery end must be timezone-aware")
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    if end <= start:
        return ()
    if not schedule or not schedule.get("sessions"):
        return ((start, end),)

    zone = ZoneInfo(str(schedule["time_zone"]))
    first_date = start.astimezone(zone).date() - timedelta(days=2)
    last_date = end.astimezone(zone).date() + timedelta(days=1)
    ranges: list[tuple[datetime, datetime]] = []
    current_date: date = first_date
    while current_date <= last_date:
        # Schedule weekdays use Sunday=0, matching the public market contract.
        schedule_weekday = (current_date.weekday() + 1) % 7
        for item in schedule["sessions"]:
            if int(item["weekday"]) != schedule_weekday:
                continue
            session_start = datetime.combine(
                current_date,
                _clock(item["open"]),
                tzinfo=zone,
            ).astimezone(UTC)
            session_end = datetime.combine(
                current_date + timedelta(days=int(item["close_day_offset"])),
                _clock(item["close"]),
                tzinfo=zone,
            ).astimezone(UTC)
            overlap_start = max(start, session_start)
            overlap_end = min(end, session_end)
            if overlap_end > overlap_start:
                ranges.append((overlap_start, overlap_end))
        current_date += timedelta(days=1)

    merged: list[tuple[datetime, datetime]] = []
    for range_start, range_end in sorted(ranges):
        if not merged or range_start > merged[-1][1]:
            merged.append((range_start, range_end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], range_end))
    return tuple(merged)


class MarketDataRecoveryCoordinator:
    """Repairs the canonical minute series after startup and realtime interruptions.

    Reads remain cache-only. Recovery requests are coalesced here and are committed by
    ``RealtimeBarService`` together with their coverage and authority metadata. Higher
    chart periods then rematerialize from the repaired minute series.
    """

    def __init__(
        self,
        bars: RecoveryBarService,
        *,
        interval: timedelta = timedelta(minutes=1),
        audit_lookback: timedelta = timedelta(days=7),
        audit_page_size: int = 10_000,
        max_request_count: int = 10_000,
        retry_base: timedelta = timedelta(seconds=1),
        retry_max: timedelta = timedelta(minutes=1),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval <= timedelta(0):
            raise ValueError("recovery interval must be positive")
        if audit_lookback <= timedelta(0):
            raise ValueError("recovery audit lookback must be positive")
        if not 1 <= audit_page_size <= 10_000:
            raise ValueError("recovery audit page size must be between 1 and 10000")
        if not 1 <= max_request_count <= 10_000:
            raise ValueError("recovery request count must be between 1 and 10000")
        if retry_base <= timedelta(0) or retry_max < retry_base:
            raise ValueError("recovery retry bounds are invalid")
        self._bars = bars
        self._interval = interval
        self._audit_lookback = audit_lookback
        self._audit_page_size = audit_page_size
        self._max_request_count = max_request_count
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._clock = clock or (lambda: datetime.now(UTC))
        self._targets: dict[tuple[str, Instrument], _RecoveryTarget] = {}
        self._started = False
        self._closed = False
        self._completed_ranges = 0
        self._recovered_rows = 0
        self._failures = 0
        self._last_failure_at: datetime | None = None
        self._last_failure_type: str | None = None

    def register_series(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        schedule: Mapping[str, Any] | None,
        seed_rows: Sequence[RealtimeBar] = (),
    ) -> None:
        if not self._bars.history_backfill_configured(source_id):
            return
        key = (source_id, instrument)
        target = self._targets.get(key)
        if target is None:
            target = _RecoveryTarget(instrument, source_id, schedule)
            self._targets[key] = target
        else:
            target.schedule = schedule
        self._inspect_rows(target, seed_rows)
        if self._started:
            self._schedule_audit(target)
            self._ensure_worker(target)

    def observe(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        observed_at: datetime,
    ) -> None:
        """Record a live source observation and enqueue only a newly exposed gap."""

        target = self._targets.get((source_id, instrument))
        if target is None:
            return
        current = self._floor(observed_at)
        previous = target.last_observed_open_time
        if previous is None or current > previous:
            target.last_observed_open_time = current
        if previous is not None and current > previous + self._interval:
            self._enqueue_expected(target, previous + self._interval, current)

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("market-data recovery coordinator is closed")
        if self._started:
            return
        self._started = True
        for target in self._targets.values():
            self._schedule_audit(target)
            self._ensure_worker(target)
        await asyncio.sleep(0)

    async def backfill(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime,
        count: int,
        revalidate: bool = False,
    ) -> BarBackfillResult:
        """Run an explicit range demand through the same calendar-aware recovery path."""

        if not 1 <= count <= 10_000:
            raise ValueError("recovery count must be between 1 and 10000")
        start = self._floor(start)
        end = start + self._interval * count
        target = self._targets.get((source_id, instrument))
        schedule = target.schedule if target is not None else None
        windows = market_open_ranges(start, end, schedule)
        if not windows:
            state = self._bars.series_state(
                instrument,
                source_id=source_id,
                interval=self._interval,
            )
            return BarBackfillResult(
                source_id=source_id,
                state=BarBackfillState.ADVANCED,
                start=start,
                end=end,
                row_count=0,
                covered_start=start,
                covered_end=end,
                authoritative_through=(
                    state.authoritative_through if state is not None else None
                ),
                history_floor=state.history_floor if state is not None else None,
                evidence_version=state.evidence_version if state is not None else None,
            )

        results: list[BarBackfillResult] = []
        try:
            for range_start, range_end in windows:
                result = await self._bars.backfill(
                    instrument,
                    source_id=source_id,
                    start=range_start,
                    count=int((range_end - range_start) / self._interval),
                    revalidate=revalidate,
                )
                results.append(result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failures += 1
            self._last_failure_at = self._now()
            self._last_failure_type = type(error).__name__
            raise

        self._completed_ranges += len(results)
        self._recovered_rows += sum(result.row_count for result in results)
        if len(results) == 1 and windows[0] == (start, end):
            return results[0]
        return self._combine_results(source_id, start, end, results)

    async def close(self) -> None:
        self._closed = True
        self._started = False
        tasks = tuple(
            task
            for target in self._targets.values()
            for task in (target.audit, target.worker)
            if task is not None and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for target in self._targets.values():
            target.audit = None
            target.worker = None

    async def wait_idle(self) -> None:
        """Wait for currently discoverable work; primarily useful for verification."""

        while True:
            tasks = tuple(
                task
                for target in self._targets.values()
                for task in (target.audit, target.worker)
                if task is not None and not task.done()
            )
            if not tasks and not any(target.pending for target in self._targets.values()):
                return
            if tasks:
                await asyncio.gather(*tasks)
            else:
                await asyncio.sleep(0)

    def metrics(self) -> MarketDataRecoveryMetrics:
        return MarketDataRecoveryMetrics(
            pending_ranges=sum(len(target.pending) for target in self._targets.values()),
            active_series=sum(
                target.worker is not None and not target.worker.done()
                for target in self._targets.values()
            ),
            completed_ranges=self._completed_ranges,
            recovered_rows=self._recovered_rows,
            failures=self._failures,
            last_failure_at=self._last_failure_at,
            last_failure_type=self._last_failure_type,
        )

    @staticmethod
    def _combine_results(
        source_id: str,
        start: datetime,
        end: datetime,
        results: Sequence[BarBackfillResult],
    ) -> BarBackfillResult:
        states = {result.state for result in results}
        if BarBackfillState.DEFERRED in states:
            state = BarBackfillState.DEFERRED
        elif BarBackfillState.FETCHED in states:
            state = BarBackfillState.FETCHED
        elif BarBackfillState.JOINED in states:
            state = BarBackfillState.JOINED
        elif states == {BarBackfillState.CACHED}:
            state = BarBackfillState.CACHED
        elif states == {BarBackfillState.EXHAUSTED}:
            state = BarBackfillState.EXHAUSTED
        else:
            state = BarBackfillState.ADVANCED
        authorities = [
            result.authoritative_through
            for result in results
            if result.authoritative_through is not None
        ]
        floors = [
            result.history_floor for result in results if result.history_floor is not None
        ]
        retries = [result.retry_after for result in results if result.retry_after is not None]
        evidence = [
            result.evidence_version for result in results if result.evidence_version is not None
        ]
        completed = state is not BarBackfillState.DEFERRED
        return BarBackfillResult(
            source_id=source_id,
            state=state,
            start=start,
            end=end,
            row_count=sum(result.row_count for result in results),
            covered_start=start if completed else None,
            covered_end=end if completed else None,
            authoritative_through=max(authorities, default=None),
            history_floor=min(floors, default=None),
            retry_after=max(retries, default=None),
            evidence_version=evidence[-1] if evidence else None,
        )

    def _schedule_audit(self, target: _RecoveryTarget) -> None:
        if target.audit is not None and not target.audit.done():
            return
        target.audit = asyncio.create_task(
            self._audit(target),
            name=f"market-data-audit:{target.source_id}:{target.instrument.symbol}",
        )

    async def _audit(self, target: _RecoveryTarget) -> None:
        try:
            now = self._floor(self._now())
            rows = await self._bars.get_bars_before(
                target.instrument,
                source_id=target.source_id,
                interval=self._interval,
                before=now,
                count=self._audit_page_size,
            )
            cutoff = now - self._audit_lookback
            self._inspect_rows(
                target,
                tuple(row for row in rows if row.open_time >= cutoff),
            )
            state = self._bars.series_state(
                target.instrument,
                source_id=target.source_id,
                interval=self._interval,
            )
            if state is not None and state.authoritative_through < now:
                self._enqueue_expected(
                    target,
                    max(state.authoritative_through, cutoff),
                    now,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failures += 1
            self._last_failure_at = self._now()
            self._last_failure_type = type(error).__name__
        finally:
            target.audit = None
            self._ensure_worker(target)

    def _inspect_rows(
        self,
        target: _RecoveryTarget,
        rows: Sequence[RealtimeBar],
    ) -> None:
        times = sorted(
            {
                self._floor(row.open_time)
                for row in rows
                if row.instrument == target.instrument
                and row.source.provider == target.source_id
                and row.interval == self._interval
            }
        )
        if not times:
            return
        for previous, current in pairwise(times):
            if current > previous + self._interval:
                self._enqueue_expected(target, previous + self._interval, current)
        latest = times[-1]
        if target.last_observed_open_time is None or latest > target.last_observed_open_time:
            target.last_observed_open_time = latest

    def _enqueue_expected(
        self,
        target: _RecoveryTarget,
        start: datetime,
        end: datetime,
    ) -> None:
        cutoff = self._floor(self._now()) - self._audit_lookback
        bounded_start = max(self._floor(start), cutoff)
        bounded_end = self._floor(end)
        for range_start, range_end in market_open_ranges(
            bounded_start,
            bounded_end,
            target.schedule,
        ):
            self._enqueue(target, range_start, range_end)
        self._ensure_worker(target)

    def _enqueue(
        self,
        target: _RecoveryTarget,
        start: datetime,
        end: datetime,
    ) -> None:
        if end <= start:
            return
        pending = sorted((*target.pending, (start, end)))
        merged: list[tuple[datetime, datetime]] = []
        for range_start, range_end in pending:
            if not merged or range_start > merged[-1][1]:
                merged.append((range_start, range_end))
                continue
            merged[-1] = (merged[-1][0], max(merged[-1][1], range_end))
        target.pending = merged

    def _ensure_worker(self, target: _RecoveryTarget) -> None:
        if not self._started or self._closed or not target.pending:
            return
        if target.worker is not None and not target.worker.done():
            return
        target.worker = asyncio.create_task(
            self._run_target(target),
            name=f"market-data-recovery:{target.source_id}:{target.instrument.symbol}",
        )

    async def _run_target(self, target: _RecoveryTarget) -> None:
        failure_count = 0
        try:
            while target.pending and not self._closed:
                start, end = target.pending.pop(0)
                maximum_end = start + self._interval * self._max_request_count
                request_end = min(end, maximum_end)
                if request_end < end:
                    target.pending.insert(0, (request_end, end))
                count = int((request_end - start) / self._interval)
                try:
                    result = await self._bars.backfill(
                        target.instrument,
                        source_id=target.source_id,
                        start=start,
                        count=count,
                        revalidate=False,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    failure_count += 1
                    self._failures += 1
                    self._last_failure_at = self._now()
                    self._last_failure_type = type(error).__name__
                    self._enqueue(target, start, request_end)
                    delay = min(
                        self._retry_base * (2 ** (failure_count - 1)),
                        self._retry_max,
                    )
                    await asyncio.sleep(delay.total_seconds())
                    continue

                if result.state is BarBackfillState.DEFERRED:
                    self._enqueue(target, start, request_end)
                    retry_after = result.retry_after
                    delay_seconds = self._retry_base.total_seconds()
                    if retry_after is not None:
                        delay_seconds = max(
                            delay_seconds,
                            (retry_after - self._now()).total_seconds(),
                        )
                    await asyncio.sleep(min(delay_seconds, self._retry_max.total_seconds()))
                    continue

                failure_count = 0
                self._completed_ranges += 1
                self._recovered_rows += result.row_count
        finally:
            target.worker = None
            if target.pending:
                self._ensure_worker(target)

    def _floor(self, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recovery timestamps must be timezone-aware")
        seconds = int(self._interval.total_seconds())
        epoch = int(value.timestamp())
        return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recovery clock must return a timezone-aware datetime")
        return value.astimezone(UTC)
