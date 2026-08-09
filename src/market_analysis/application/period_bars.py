from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from market_analysis.application.realtime_bars import RealtimeBarService
from market_analysis.domain.market_events import BarState, RealtimeBar
from market_analysis.domain.models import Instrument, SourceMetadata


@dataclass(frozen=True, slots=True)
class PeriodDefinition:
    period_id: str
    minutes: int | None = None
    calendar_unit: str | None = None


PERIOD_DEFINITIONS: dict[str, PeriodDefinition] = {
    "timeline": PeriodDefinition("timeline", minutes=1),
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

_MINUTE_PAGE_SIZE = 10_000
_BAR_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class PeriodBarPage:
    period_id: str
    items: tuple[RealtimeBar, ...]
    next_before: datetime | None
    has_more: bool


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
    if definition.minutes is not None:
        duration = timedelta(minutes=definition.minutes)
        if occurrence is not None:
            elapsed = value.astimezone(UTC) - occurrence.start.astimezone(UTC)
            offset = int(elapsed / duration)
            start = occurrence.start + duration * offset
            end = min(occurrence.end, start + duration)
        else:
            seconds = definition.minutes * 60
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
) -> tuple[RealtimeBar, ...]:
    definition = PERIOD_DEFINITIONS.get(period_id)
    if definition is None:
        raise ValueError(f"unsupported chart period {period_id!r}")
    observed_now = (now or datetime.now(UTC)).astimezone(UTC)
    grouped: dict[str, tuple[_Bucket, list[RealtimeBar]]] = {}
    for row in sorted(rows, key=lambda item: item.open_time):
        bucket = _bucket_for(row.open_time, definition, schedule)
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
            timedelta(minutes=definition.minutes)
            if definition.minutes is not None
            else bucket.end - bucket.start
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


class PeriodBarService:
    """Builds schedule-aware chart Bars from the canonical one-minute projection."""

    def __init__(self, realtime_bars: RealtimeBarService) -> None:
        self._realtime_bars = realtime_bars

    async def get_page(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: Mapping[str, Any] | None,
        before: datetime | None = None,
    ) -> PeriodBarPage:
        if period_id not in PERIOD_DEFINITIONS:
            raise ValueError(f"unsupported chart period {period_id!r}")
        cursor = before
        minute_rows: dict[datetime, RealtimeBar] = {}
        exhausted = False
        projected: tuple[RealtimeBar, ...] = ()
        while len(projected) <= _BAR_PAGE_SIZE:
            page = await self._realtime_bars.get_bars_before(
                instrument,
                source_id=source_id,
                before=cursor,
                count=_MINUTE_PAGE_SIZE,
            )
            if not page:
                exhausted = True
                break
            for row in page:
                minute_rows[row.open_time] = row
            projected = project_period_bars(
                tuple(minute_rows.values()),
                period_id=period_id,
                schedule=schedule,
            )
            next_cursor = page[0].open_time
            if cursor is not None and next_cursor >= cursor:
                raise RuntimeError("minute Bar cursor did not advance")
            cursor = next_cursor

        if not projected and minute_rows:
            projected = project_period_bars(
                tuple(minute_rows.values()),
                period_id=period_id,
                schedule=schedule,
            )
        items = projected[-_BAR_PAGE_SIZE:]
        oldest_component = None
        if items:
            payload = items[0].source.raw_payload or {}
            raw_value = payload.get("bucket_first_open_time")
            if isinstance(raw_value, str):
                oldest_component = datetime.fromisoformat(raw_value).astimezone(UTC)
            else:
                oldest_component = items[0].open_time
        return PeriodBarPage(
            period_id=period_id,
            items=items,
            next_before=oldest_component,
            has_more=len(projected) > _BAR_PAGE_SIZE or not exhausted,
        )
