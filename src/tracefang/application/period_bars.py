from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from tracefang.application.realtime_bars import (
    REALTIME_BAR_READ_PAGE_SIZE_MAX,
    RealtimeBarService,
)
from tracefang.domain.market_events import BarState, RealtimeBar
from tracefang.domain.models import Instrument, SourceMetadata


@dataclass(frozen=True, slots=True)
class PeriodDefinition:
    period_id: str
    seconds: int | None = None
    minutes: int | None = None
    calendar_unit: str | None = None


PERIOD_DEFINITIONS: dict[str, PeriodDefinition] = {
    "timeline": PeriodDefinition("timeline", seconds=1),
    "1s": PeriodDefinition("1s", seconds=1),
    "1m": PeriodDefinition("1m", minutes=1),
    "3m": PeriodDefinition("3m", minutes=3),
    "5m": PeriodDefinition("5m", minutes=5),
    "10m": PeriodDefinition("10m", minutes=10),
    "15m": PeriodDefinition("15m", minutes=15),
    "30m": PeriodDefinition("30m", minutes=30),
    "1h": PeriodDefinition("1h", minutes=60),
    "2h": PeriodDefinition("2h", minutes=120),
    "4h": PeriodDefinition("4h", minutes=240),
    "6h": PeriodDefinition("6h", minutes=360),
    "8h": PeriodDefinition("8h", minutes=480),
    "12h": PeriodDefinition("12h", minutes=720),
    "1d": PeriodDefinition("1d", calendar_unit="day"),
    "1w": PeriodDefinition("1w", calendar_unit="week"),
    "1mo": PeriodDefinition("1mo", calendar_unit="month"),
    "1q": PeriodDefinition("1q", calendar_unit="quarter"),
    "1y": PeriodDefinition("1y", calendar_unit="year"),
}

_DEFAULT_BAR_PAGE_SIZE = 500
_PERIOD_BAR_MATERIALIZATION_ALGORITHM = "period-bars-v1"
_MAX_READ_OVERLAY_MUTATIONS = 20_000


@dataclass(frozen=True, slots=True)
class PeriodBarPage:
    period_id: str
    items: tuple[RealtimeBar, ...]
    next_before: datetime | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class PeriodBarInputChange:
    mutation_id: int
    open_time: datetime


@dataclass(frozen=True, slots=True)
class PeriodBarBucketAggregate:
    first_open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    all_final: bool
    any_authoritative: bool
    finalized_at: datetime | None
    revision: int
    component_count: int
    provider_symbol: str
    evidence_channel_id: str
    observed_at: datetime
    received_at: datetime


@dataclass(frozen=True, slots=True)
class PeriodBarMaterializationState:
    source_cursor: datetime | None = None
    oldest_bucket_open_time: datetime | None = None
    history_exhausted: bool = False
    processed_mutation_id: int = 0


class PeriodBarStore(Protocol):
    async def load_period_bar_materialization(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        materialization_version: str,
    ) -> PeriodBarMaterializationState | None: ...

    async def save_period_bar_materialization(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        materialization_version: str,
        state: PeriodBarMaterializationState,
    ) -> None: ...

    async def load_materialized_period_bars_before(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        materialization_version: str,
        before: datetime | None,
        count: int,
    ) -> tuple[RealtimeBar, ...]: ...

    async def save_materialized_period_bars(
        self,
        bars: Sequence[RealtimeBar],
        *,
        source_id: str,
        period_id: str,
        materialization_version: str,
    ) -> None: ...

    async def delete_materialized_period_bar(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        materialization_version: str,
        open_time: datetime,
    ) -> None: ...

    async def latest_realtime_bar_mutation_id(
        self,
        instrument: Instrument,
        *,
        source_id: str,
    ) -> int: ...

    async def load_realtime_bar_input_changes(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        after_mutation_id: int,
        through_mutation_id: int,
        count: int,
    ) -> tuple[PeriodBarInputChange, ...]: ...

    async def aggregate_realtime_bar_bucket(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime,
        end: datetime,
    ) -> PeriodBarBucketAggregate | None: ...


@dataclass(frozen=True, slots=True)
class _SessionOccurrence:
    start: datetime
    end: datetime
    trading_date: date


@dataclass(frozen=True, slots=True)
class _Bucket:
    key: str
    start: datetime
    end: datetime


def _clock(value: object) -> time:
    if not isinstance(value, str):
        raise ValueError("market schedule clock must be a string")
    hour_text, minute_text = value.split(":", maxsplit=1)
    return time(hour=int(hour_text), minute=int(minute_text))


def _next_weekday(value: date) -> date:
    result = value
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def _trading_date_for(start: datetime, end: datetime, rule: str) -> date:
    start_date = start.date()
    if rule == "shfe":
        return (
            _next_weekday(start_date + timedelta(days=1))
            if start.timetz().replace(tzinfo=None) >= time(18)
            else start_date
        )
    if rule == "session_start":
        return start_date
    return (end - timedelta(microseconds=1)).date()


def _session_occurrence(
    value: datetime,
    schedule: Mapping[str, Any] | None,
) -> _SessionOccurrence | None:
    if not schedule or not schedule.get("sessions"):
        return None
    zone = ZoneInfo(str(schedule["time_zone"]))
    local_value = value.astimezone(zone)
    rule = str(schedule.get("trading_day_rule", "session_end"))
    for day_offset in range(3):
        start_date = local_value.date() - timedelta(days=day_offset)
        schedule_weekday = (start_date.weekday() + 1) % 7
        for item in schedule["sessions"]:
            if int(item["weekday"]) != schedule_weekday:
                continue
            start = datetime.combine(start_date, _clock(item["open"]), tzinfo=zone)
            end_date = start_date + timedelta(days=int(item["close_day_offset"]))
            end = datetime.combine(end_date, _clock(item["close"]), tzinfo=zone)
            if not start <= local_value < end:
                continue
            trading_date = _trading_date_for(start, end, rule)
            return _SessionOccurrence(start, end, trading_date)
    return None


def _trading_day_end(
    trading_date: date,
    schedule: Mapping[str, Any] | None,
) -> datetime | None:
    if not schedule or not schedule.get("sessions"):
        return None
    zone = ZoneInfo(str(schedule["time_zone"]))
    rule = str(schedule.get("trading_day_rule", "session_end"))
    ends: list[datetime] = []
    for day_offset in range(-3, 2):
        start_date = trading_date + timedelta(days=day_offset)
        schedule_weekday = (start_date.weekday() + 1) % 7
        for item in schedule["sessions"]:
            if int(item["weekday"]) != schedule_weekday:
                continue
            start = datetime.combine(start_date, _clock(item["open"]), tzinfo=zone)
            end = datetime.combine(
                start_date + timedelta(days=int(item["close_day_offset"])),
                _clock(item["close"]),
                tzinfo=zone,
            )
            if _trading_date_for(start, end, rule) == trading_date:
                ends.append(end.astimezone(UTC))
    return max(ends, default=None)


def _calendar_bounds(value: date, unit: str) -> tuple[date, date]:
    if unit == "day":
        return value, value + timedelta(days=1)
    if unit == "week":
        start = value - timedelta(days=value.weekday())
        return start, start + timedelta(days=7)
    if unit == "month":
        start = value.replace(day=1)
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        return start, end
    if unit == "quarter":
        month = (value.month - 1) // 3 * 3 + 1
        start = date(value.year, month, 1)
        end_month = month + 3
        end = date(value.year + (end_month > 12), (end_month - 1) % 12 + 1, 1)
        return start, end
    if unit == "year":
        return date(value.year, 1, 1), date(value.year + 1, 1, 1)
    raise ValueError(f"unsupported calendar period {unit!r}")


def _bucket_for(
    value: datetime,
    definition: PeriodDefinition,
    schedule: Mapping[str, Any] | None,
) -> _Bucket:
    occurrence = _session_occurrence(value, schedule)
    zone = ZoneInfo(str(schedule["time_zone"])) if schedule else UTC
    if definition.seconds is not None or definition.minutes is not None:
        duration = (
            timedelta(seconds=definition.seconds)
            if definition.seconds is not None
            else timedelta(minutes=definition.minutes or 0)
        )
        if occurrence is not None:
            elapsed = value.astimezone(UTC) - occurrence.start.astimezone(UTC)
            offset = int(elapsed / duration)
            start = occurrence.start + duration * offset
            end = min(occurrence.end, start + duration)
        else:
            seconds = int(duration.total_seconds())
            epoch = int(value.timestamp())
            start = datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)
            end = start + duration
        utc_start = start.astimezone(UTC)
        return _Bucket(f"fixed:{utc_start.isoformat()}", utc_start, end.astimezone(UTC))

    trading_date = occurrence.trading_date if occurrence else value.astimezone(zone).date()
    start_date, end_date = _calendar_bounds(trading_date, str(definition.calendar_unit))
    start = datetime.combine(start_date, time(), tzinfo=zone).astimezone(UTC)
    end = datetime.combine(end_date, time(), tzinfo=zone).astimezone(UTC)
    if definition.calendar_unit == "day":
        end = _trading_day_end(trading_date, schedule) or end
    return _Bucket(f"calendar:{definition.calendar_unit}:{start_date.isoformat()}", start, end)


def project_period_bars(
    rows: Sequence[RealtimeBar],
    *,
    period_id: str,
    schedule: Mapping[str, Any] | None,
    now: datetime | None = None,
    _resolved_buckets: Mapping[datetime, _Bucket] | None = None,
) -> tuple[RealtimeBar, ...]:
    definition = PERIOD_DEFINITIONS.get(period_id)
    if definition is None:
        raise ValueError(f"unsupported chart period {period_id!r}")
    observed_now = (now or datetime.now(UTC)).astimezone(UTC)
    grouped: dict[str, tuple[_Bucket, list[RealtimeBar]]] = {}
    for row in sorted(rows, key=lambda item: item.open_time):
        bucket = (
            _resolved_buckets.get(row.open_time) if _resolved_buckets is not None else None
        ) or _bucket_for(row.open_time, definition, schedule)
        grouped.setdefault(bucket.key, (bucket, []))[1].append(row)

    projected: list[RealtimeBar] = []
    for bucket, members in grouped.values():
        first = members[0]
        latest = max(members, key=lambda item: (item.open_time, item.source.received_at))
        all_final = all(item.state is BarState.FINAL for item in members)
        if all_final and observed_now >= bucket.end:
            state = BarState.FINAL
        elif any(item.state is not BarState.PROVISIONAL_QUOTE for item in members):
            state = BarState.PROVISIONAL_AUTHORITATIVE
        else:
            state = BarState.PROVISIONAL_QUOTE
        finalized_at = (
            max(item.finalized_at or item.source.received_at for item in members)
            if state is BarState.FINAL
            else None
        )
        volumes = [item.volume for item in members if item.volume is not None]
        interval = (
            timedelta(seconds=definition.seconds)
            if definition.seconds is not None
            else (
                timedelta(minutes=definition.minutes)
                if definition.minutes is not None
                else bucket.end - bucket.start
            )
        )
        projected.append(
            RealtimeBar(
                instrument=first.instrument,
                interval=interval,
                open_time=bucket.start,
                open=first.open,
                high=max(item.high for item in members),
                low=min(item.low for item in members),
                close=latest.close,
                volume=sum(volumes, Decimal(0)) if volumes else None,
                source=SourceMetadata(
                    provider=latest.source.provider,
                    provider_symbol=latest.source.provider_symbol,
                    observed_at=max(item.source.observed_at for item in members),
                    received_at=max(item.source.received_at for item in members),
                    raw_payload={
                        "derivation": "backend_period_projection",
                        "period_id": period_id,
                        "bucket_first_open_time": first.open_time.isoformat(),
                        "bucket_end": bucket.end.isoformat(),
                        "component_count": len(members),
                    },
                ),
                evidence_channel_id=latest.evidence_channel_id,
                state=state,
                revision=sum(item.revision for item in members),
                finalized_at=finalized_at,
            )
        )
    return tuple(sorted(projected, key=lambda item: item.open_time))


def _materialization_version(schedule: Mapping[str, Any] | None) -> str:
    serialized = json.dumps(
        {
            "algorithm": _PERIOD_BAR_MATERIALIZATION_ALGORITHM,
            "schedule": schedule,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:20]
    return f"{_PERIOD_BAR_MATERIALIZATION_ALGORITHM}:{digest}"


def _payload_time(bar: RealtimeBar, field: str, default: datetime) -> datetime:
    value = (bar.source.raw_payload or {}).get(field)
    if not isinstance(value, str):
        return default
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"materialized period Bar {field} must be timezone-aware")
    return parsed.astimezone(UTC)


class PeriodBarService:
    """Materializes schedule-aware chart Bars from the canonical minute series."""

    def __init__(
        self,
        realtime_bars: RealtimeBarService,
        *,
        store: PeriodBarStore | None = None,
    ) -> None:
        self._realtime_bars = realtime_bars
        self._store = store
        self._locks: dict[tuple[str, Instrument, str, str], asyncio.Lock] = {}
        self._live_components: dict[
            tuple[str, Instrument, str, str], dict[datetime, RealtimeBar]
        ] = {}
        self._live_buckets: dict[tuple[str, Instrument, str], _Bucket] = {}
        self._live_minutes: dict[tuple[str, Instrument], dict[datetime, RealtimeBar]] = {}

    def seed_live(
        self,
        rows: Sequence[RealtimeBar],
        *,
        schedule: Mapping[str, Any] | None,
    ) -> None:
        """Seeds the bounded live projector from canonical minute Bars."""

        for row in sorted(rows, key=lambda item: item.open_time):
            self.accept_live(row, schedule=schedule, period_ids=())

    def accept_live(
        self,
        bar: RealtimeBar,
        *,
        schedule: Mapping[str, Any] | None,
        period_ids: Iterable[str] | None = None,
    ) -> tuple[tuple[str, RealtimeBar], ...]:
        """Projects one complete minute-Bar upsert into every chart period.

        The cache retains only the active bucket for each period. Corrections of the
        active minute replace that component before the aggregate is recomputed, so
        a lower corrected high/low cannot leak from an earlier revision.
        """

        if bar.interval != timedelta(minutes=1):
            return ()
        source_id = bar.source.provider
        minute_key = (source_id, bar.instrument)
        minute_rows = self._live_minutes.setdefault(minute_key, {})
        previous = minute_rows.get(bar.open_time)
        if previous is None or (
            bar.revision > previous.revision
            or (
                bar.revision == previous.revision
                and bar.source.received_at >= previous.source.received_at
            )
        ):
            minute_rows[bar.open_time] = bar
        overflow = len(minute_rows) - REALTIME_BAR_READ_PAGE_SIZE_MAX
        if overflow > 0:
            for open_time in sorted(minute_rows)[:overflow]:
                minute_rows.pop(open_time, None)

        requested = (
            tuple(period_ids)
            if period_ids is not None
            else tuple(
                period_id
                for period_id in PERIOD_DEFINITIONS
                if period_id not in {"timeline", "1s", "1m"}
            )
        )
        values: list[tuple[str, RealtimeBar]] = []
        for period_id in requested:
            definition = PERIOD_DEFINITIONS.get(period_id)
            if definition is None or period_id in {"timeline", "1s", "1m"}:
                raise ValueError(f"unsupported live chart period {period_id!r}")
            bucket = _bucket_for(bar.open_time, definition, schedule)
            series_key = (source_id, bar.instrument, period_id)
            active = self._live_buckets.get(series_key)
            if active is not None and bucket.start < active.start:
                continue
            if active is None or bucket.key != active.key:
                for key in tuple(self._live_components):
                    if key[:3] == series_key:
                        self._live_components.pop(key, None)
                self._live_buckets[series_key] = bucket
            component_key = (*series_key, bucket.key)
            components = self._live_components.get(component_key)
            if components is None:
                components = {
                    open_time: value
                    for open_time, value in minute_rows.items()
                    if _bucket_for(open_time, definition, schedule).key == bucket.key
                }
                self._live_components[component_key] = components
            current = components.get(bar.open_time)
            if current is not None and (
                bar.revision < current.revision
                or (
                    bar.revision == current.revision
                    and bar.source.received_at < current.source.received_at
                )
            ):
                continue
            components[bar.open_time] = bar
            projected = project_period_bars(
                tuple(components.values()),
                period_id=period_id,
                schedule=schedule,
                now=bar.source.received_at,
            )
            value = next(
                (candidate for candidate in projected if candidate.open_time == bucket.start),
                None,
            )
            if value is not None:
                values.append((period_id, value))
        return tuple(values)

    async def get_page(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: Mapping[str, Any] | None,
        before: datetime | None = None,
        page_size: int = _DEFAULT_BAR_PAGE_SIZE,
    ) -> PeriodBarPage:
        if period_id not in PERIOD_DEFINITIONS:
            raise ValueError(f"unsupported chart period {period_id!r}")
        if page_size < 1:
            raise ValueError("page_size must be positive")
        if before is not None and (before.tzinfo is None or before.utcoffset() is None):
            raise ValueError("before must be timezone-aware")
        # Query endpoints are deliberately read-only. Expensive precomputation, if
        # introduced later, belongs to an explicit background command and must not
        # be triggered by an HTTP GET.
        materialized = await self._load_current_materialized_page(
            instrument,
            source_id=source_id,
            period_id=period_id,
            schedule=schedule,
            before=before,
            page_size=page_size,
        )
        if materialized is not None:
            return materialized
        return await self._get_unmaterialized_page(
            instrument,
            source_id=source_id,
            period_id=period_id,
            schedule=schedule,
            before=before,
            page_size=page_size,
        )

    async def _load_current_materialized_page(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: Mapping[str, Any] | None,
        before: datetime | None,
        page_size: int,
    ) -> PeriodBarPage | None:
        definition = PERIOD_DEFINITIONS[period_id]
        if self._store is None or definition.seconds is not None or period_id == "1m":
            return None
        materialization_version = _materialization_version(schedule)
        state = await self._store.load_period_bar_materialization(
            instrument,
            source_id=source_id,
            period_id=period_id,
            materialization_version=materialization_version,
        )
        if state is None:
            return None
        target_mutation_id = await self._store.latest_realtime_bar_mutation_id(
            instrument,
            source_id=source_id,
        )
        affected: dict[str, _Bucket] = {}
        mutation_cursor = state.processed_mutation_id
        mutation_count = 0
        definition = PERIOD_DEFINITIONS[period_id]
        while mutation_cursor < target_mutation_id:
            changes = await self._store.load_realtime_bar_input_changes(
                instrument,
                source_id=source_id,
                after_mutation_id=mutation_cursor,
                through_mutation_id=target_mutation_id,
                count=min(
                    REALTIME_BAR_READ_PAGE_SIZE_MAX,
                    _MAX_READ_OVERLAY_MUTATIONS - mutation_count,
                ),
            )
            if not changes:
                break
            mutation_count += len(changes)
            for change in changes:
                bucket = _bucket_for(change.open_time, definition, schedule)
                if (
                    state.oldest_bucket_open_time is None
                    or bucket.start < state.oldest_bucket_open_time
                ):
                    return None
                if before is None or bucket.start < before:
                    affected[bucket.key] = bucket
            next_cursor = max(item.mutation_id for item in changes)
            if next_cursor <= mutation_cursor:
                return None
            mutation_cursor = next_cursor
            if (
                mutation_count >= _MAX_READ_OVERLAY_MUTATIONS
                and mutation_cursor < target_mutation_id
            ):
                return None
        values = await self._store.load_materialized_period_bars_before(
            instrument,
            source_id=source_id,
            period_id=period_id,
            materialization_version=materialization_version,
            before=before,
            count=page_size + len(affected) + 1,
        )
        rows = {value.open_time: value for value in values}
        for bucket in affected.values():
            value = await self._project_bucket(
                instrument,
                source_id=source_id,
                period_id=period_id,
                schedule=schedule,
                bucket=bucket,
            )
            if value is None:
                rows.pop(bucket.start, None)
            else:
                rows[bucket.start] = value
        ordered = tuple(
            value
            for value in sorted(rows.values(), key=lambda item: item.open_time)
            if before is None or value.open_time < before
        )
        if not ordered and not state.history_exhausted:
            return None
        items = ordered[-page_size:]
        oldest_component = (
            _payload_time(items[0], "bucket_first_open_time", items[0].open_time) if items else None
        )
        return PeriodBarPage(
            period_id=period_id,
            items=items,
            next_before=oldest_component,
            has_more=len(ordered) > page_size or not state.history_exhausted,
        )

    async def materialize_page(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: Mapping[str, Any] | None,
        before: datetime | None = None,
        page_size: int = _DEFAULT_BAR_PAGE_SIZE,
    ) -> PeriodBarPage:
        """Explicitly prepares one derived-period page and returns the persisted result.

        This is a command-side operation used after an accepted history demand. It
        keeps ordinary chart GETs read-only while making repeated large-period
        reads independent of the size of the canonical minute history.
        """

        if period_id not in PERIOD_DEFINITIONS:
            raise ValueError(f"unsupported chart period {period_id!r}")
        if page_size < 1:
            raise ValueError("page_size must be positive")
        if before is not None and (before.tzinfo is None or before.utcoffset() is None):
            raise ValueError("before must be timezone-aware")
        definition = PERIOD_DEFINITIONS[period_id]
        if self._store is None or definition.seconds is not None or period_id == "1m":
            return await self._get_unmaterialized_page(
                instrument,
                source_id=source_id,
                period_id=period_id,
                schedule=schedule,
                before=before,
                page_size=page_size,
            )

        materialization_version = _materialization_version(schedule)
        key = (source_id, instrument, period_id, materialization_version)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            state = (
                await self._store.load_period_bar_materialization(
                    instrument,
                    source_id=source_id,
                    period_id=period_id,
                    materialization_version=materialization_version,
                )
                or PeriodBarMaterializationState()
            )
            target_mutation_id = await self._store.latest_realtime_bar_mutation_id(
                instrument,
                source_id=source_id,
            )
            refreshed = await self._refresh_changed_buckets(
                instrument,
                source_id=source_id,
                period_id=period_id,
                schedule=schedule,
                materialization_version=materialization_version,
                state=state,
                target_mutation_id=target_mutation_id,
            )
            if refreshed != state:
                state = refreshed
                await self._store.save_period_bar_materialization(
                    instrument,
                    source_id=source_id,
                    period_id=period_id,
                    materialization_version=materialization_version,
                    state=state,
                )

            values: tuple[RealtimeBar, ...] = ()
            while True:
                values = await self._store.load_materialized_period_bars_before(
                    instrument,
                    source_id=source_id,
                    period_id=period_id,
                    materialization_version=materialization_version,
                    before=before,
                    count=page_size + 1,
                )
                if len(values) > page_size or state.history_exhausted:
                    break
                next_state = await self._materialize_next_history_chunk(
                    instrument,
                    source_id=source_id,
                    period_id=period_id,
                    schedule=schedule,
                    materialization_version=materialization_version,
                    state=state,
                    remaining_bars=page_size + 1 - len(values),
                )
                if next_state == state:
                    break
                state = next_state
                await self._store.save_period_bar_materialization(
                    instrument,
                    source_id=source_id,
                    period_id=period_id,
                    materialization_version=materialization_version,
                    state=state,
                )

        items = values[-page_size:]
        oldest_component = (
            _payload_time(items[0], "bucket_first_open_time", items[0].open_time) if items else None
        )
        return PeriodBarPage(
            period_id=period_id,
            items=items,
            next_before=oldest_component,
            has_more=len(values) > page_size or not state.history_exhausted,
        )

    async def _refresh_changed_buckets(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: Mapping[str, Any] | None,
        materialization_version: str,
        state: PeriodBarMaterializationState,
        target_mutation_id: int,
    ) -> PeriodBarMaterializationState:
        if target_mutation_id <= state.processed_mutation_id:
            return state
        if state.oldest_bucket_open_time is None and not state.history_exhausted:
            return replace(state, processed_mutation_id=target_mutation_id)

        cursor = state.processed_mutation_id
        affected: dict[str, _Bucket] = {}
        discovered_older_input = False
        definition = PERIOD_DEFINITIONS[period_id]
        while cursor < target_mutation_id:
            changes = await self._store.load_realtime_bar_input_changes(
                instrument,
                source_id=source_id,
                after_mutation_id=cursor,
                through_mutation_id=target_mutation_id,
                count=REALTIME_BAR_READ_PAGE_SIZE_MAX,
            )
            if not changes:
                break
            for change in changes:
                bucket = _bucket_for(change.open_time, definition, schedule)
                if (
                    state.oldest_bucket_open_time is None
                    or bucket.start < state.oldest_bucket_open_time
                ):
                    discovered_older_input = True
                else:
                    affected[bucket.key] = bucket
            next_cursor = max(item.mutation_id for item in changes)
            if next_cursor <= cursor:
                raise RuntimeError("realtime Bar mutation cursor did not advance")
            cursor = next_cursor

        for bucket in sorted(affected.values(), key=lambda item: item.start):
            await self._recompute_bucket(
                instrument,
                source_id=source_id,
                period_id=period_id,
                schedule=schedule,
                materialization_version=materialization_version,
                bucket=bucket,
            )
        return replace(
            state,
            history_exhausted=state.history_exhausted and not discovered_older_input,
            processed_mutation_id=target_mutation_id,
        )

    async def _recompute_bucket(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: Mapping[str, Any] | None,
        materialization_version: str,
        bucket: _Bucket,
    ) -> None:
        value = await self._project_bucket(
            instrument,
            source_id=source_id,
            period_id=period_id,
            schedule=schedule,
            bucket=bucket,
        )
        if value is None:
            await self._store.delete_materialized_period_bar(
                instrument,
                source_id=source_id,
                period_id=period_id,
                materialization_version=materialization_version,
                open_time=bucket.start,
            )
            return
        await self._store.save_materialized_period_bars(
            (value,),
            source_id=source_id,
            period_id=period_id,
            materialization_version=materialization_version,
        )

    async def _project_bucket(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: Mapping[str, Any] | None,
        bucket: _Bucket,
    ) -> RealtimeBar | None:
        aggregate_loader = getattr(self._store, "aggregate_realtime_bar_bucket", None)
        if callable(aggregate_loader):
            aggregate = await aggregate_loader(
                instrument,
                source_id=source_id,
                start=bucket.start,
                end=bucket.end,
            )
            if aggregate is None:
                return None
            definition = PERIOD_DEFINITIONS[period_id]
            now = datetime.now(UTC)
            if aggregate.all_final and now >= bucket.end:
                state = BarState.FINAL
            elif aggregate.any_authoritative:
                state = BarState.PROVISIONAL_AUTHORITATIVE
            else:
                state = BarState.PROVISIONAL_QUOTE
            interval = (
                timedelta(minutes=definition.minutes)
                if definition.minutes is not None
                else bucket.end - bucket.start
            )
            return RealtimeBar(
                instrument=instrument,
                interval=interval,
                open_time=bucket.start,
                open=aggregate.open,
                high=aggregate.high,
                low=aggregate.low,
                close=aggregate.close,
                volume=aggregate.volume,
                source=SourceMetadata(
                    provider=source_id,
                    provider_symbol=aggregate.provider_symbol,
                    observed_at=aggregate.observed_at,
                    received_at=aggregate.received_at,
                    raw_payload={
                        "derivation": "backend_period_projection",
                        "period_id": period_id,
                        "bucket_first_open_time": aggregate.first_open_time.isoformat(),
                        "bucket_end": bucket.end.isoformat(),
                        "component_count": aggregate.component_count,
                    },
                ),
                evidence_channel_id=aggregate.evidence_channel_id,
                state=state,
                revision=aggregate.revision,
                finalized_at=aggregate.finalized_at if state is BarState.FINAL else None,
            )

        rows = await self._load_bucket_rows(
            instrument,
            source_id=source_id,
            period_id=period_id,
            schedule=schedule,
            bucket=bucket,
        )
        projected = project_period_bars(
            rows,
            period_id=period_id,
            schedule=schedule,
        )
        return next((item for item in projected if item.open_time == bucket.start), None)

    async def _load_bucket_rows(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: Mapping[str, Any] | None,
        bucket: _Bucket,
    ) -> tuple[RealtimeBar, ...]:
        definition = PERIOD_DEFINITIONS[period_id]
        cursor = bucket.end
        rows: dict[datetime, RealtimeBar] = {}
        bucket_minutes = max(
            1,
            int((bucket.end - bucket.start) / timedelta(minutes=1)),
        )
        detection_minutes = bucket_minutes + (
            24 * 60 + 1 if definition.calendar_unit is not None else 1
        )
        transport_count = min(
            REALTIME_BAR_READ_PAGE_SIZE_MAX,
            detection_minutes,
        )
        while True:
            page = await self._realtime_bars.get_bars_before(
                instrument,
                source_id=source_id,
                before=cursor,
                count=transport_count,
            )
            if not page:
                break
            crossed_bucket_start = False
            for row in page:
                candidate = _bucket_for(row.open_time, definition, schedule)
                if candidate.key == bucket.key:
                    rows[row.open_time] = row
                elif candidate.start < bucket.start:
                    crossed_bucket_start = True
            next_cursor = page[0].open_time
            if next_cursor >= cursor:
                raise RuntimeError("minute Bar bucket cursor did not advance")
            cursor = next_cursor
            if crossed_bucket_start:
                break
        return tuple(sorted(rows.values(), key=lambda item: item.open_time))

    async def _materialize_next_history_chunk(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: Mapping[str, Any] | None,
        materialization_version: str,
        state: PeriodBarMaterializationState,
        remaining_bars: int,
    ) -> PeriodBarMaterializationState:
        definition = PERIOD_DEFINITIONS[period_id]
        estimated_minutes_per_bar = definition.minutes or 24 * 60
        transport_count = min(
            REALTIME_BAR_READ_PAGE_SIZE_MAX,
            max(1, (remaining_bars + 1) * estimated_minutes_per_bar),
        )
        cursor = state.source_cursor
        minute_rows: dict[datetime, RealtimeBar] = {}
        bucket_keys: set[str] = set()
        exhausted = False
        while len(bucket_keys) < 2:
            page = await self._realtime_bars.get_bars_before(
                instrument,
                source_id=source_id,
                before=cursor,
                count=transport_count,
            )
            if not page:
                exhausted = True
                break
            for row in page:
                minute_rows[row.open_time] = row
                bucket_keys.add(_bucket_for(row.open_time, definition, schedule).key)
            next_cursor = page[0].open_time
            if cursor is not None and next_cursor >= cursor:
                raise RuntimeError("minute Bar materialization cursor did not advance")
            cursor = next_cursor

        projected = project_period_bars(
            tuple(minute_rows.values()),
            period_id=period_id,
            schedule=schedule,
        )
        stable = projected if exhausted else projected[1:]
        if stable:
            await self._store.save_materialized_period_bars(
                stable,
                source_id=source_id,
                period_id=period_id,
                materialization_version=materialization_version,
            )
            oldest = stable[0]
            source_cursor = _payload_time(
                oldest,
                "bucket_first_open_time",
                oldest.open_time,
            )
            oldest_bucket = (
                oldest.open_time
                if state.oldest_bucket_open_time is None
                else min(state.oldest_bucket_open_time, oldest.open_time)
            )
        else:
            source_cursor = state.source_cursor
            oldest_bucket = state.oldest_bucket_open_time
        return replace(
            state,
            source_cursor=source_cursor,
            oldest_bucket_open_time=oldest_bucket,
            history_exhausted=state.history_exhausted or exhausted,
        )

    async def _get_unmaterialized_page(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: Mapping[str, Any] | None,
        before: datetime | None,
        page_size: int,
    ) -> PeriodBarPage:
        definition = PERIOD_DEFINITIONS[period_id]
        if definition.seconds is not None:
            values = await self._realtime_bars.get_bars_before(
                instrument,
                source_id=source_id,
                interval=timedelta(seconds=definition.seconds),
                before=before,
                count=min(REALTIME_BAR_READ_PAGE_SIZE_MAX, page_size + 1),
            )
            items = values[-page_size:]
            return PeriodBarPage(
                period_id=period_id,
                items=items,
                next_before=items[0].open_time if items else None,
                has_more=len(values) > page_size,
            )
        estimated_minutes_per_bar = definition.minutes or 24 * 60
        cursor = before
        minute_rows: dict[datetime, RealtimeBar] = {}
        resolved_buckets: dict[datetime, _Bucket] = {}
        bucket_keys: set[str] = set()
        exhausted = False
        while len(bucket_keys) <= page_size:
            remaining_bars = page_size + 1 - len(bucket_keys)
            minute_transport_size = max(
                1,
                min(
                    REALTIME_BAR_READ_PAGE_SIZE_MAX,
                    remaining_bars * estimated_minutes_per_bar,
                ),
            )
            page = await self._realtime_bars.get_bars_before(
                instrument,
                source_id=source_id,
                before=cursor,
                count=minute_transport_size,
            )
            if not page:
                exhausted = True
                break
            for row in page:
                minute_rows[row.open_time] = row
                bucket = _bucket_for(row.open_time, definition, schedule)
                resolved_buckets[row.open_time] = bucket
                bucket_keys.add(bucket.key)
            next_cursor = page[0].open_time
            if cursor is not None and next_cursor >= cursor:
                raise RuntimeError("minute Bar cursor did not advance")
            cursor = next_cursor

        projected = (
            project_period_bars(
                tuple(minute_rows.values()),
                period_id=period_id,
                schedule=schedule,
                _resolved_buckets=resolved_buckets,
            )
            if minute_rows
            else ()
        )
        items = projected[-page_size:]
        oldest_component = (
            _payload_time(items[0], "bucket_first_open_time", items[0].open_time) if items else None
        )
        return PeriodBarPage(
            period_id=period_id,
            items=items,
            next_before=oldest_component,
            has_more=len(projected) > page_size or not exhausted,
        )
