from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from tracefang.application.period_bars import (
    PERIOD_DEFINITIONS,
    PeriodBarPage,
)
from tracefang.application.realtime_bars import (
    BarBackfillResult,
    BarBackfillState,
)
from tracefang.domain.models import Instrument

_MAX_TRANSPORT_MINUTES = 10_000
_CALENDAR_PERIOD_MINUTES = {
    "day": 24 * 60,
    "week": 7 * 24 * 60,
    "month": 31 * 24 * 60,
    "quarter": 92 * 24 * 60,
    "year": 366 * 24 * 60,
}


def _schedule_cursor_version(schedule: dict[str, Any] | None) -> str:
    payload = json.dumps(
        schedule,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def encode_chart_page_cursor(
    instrument: Instrument,
    *,
    source_id: str,
    period_id: str,
    schedule: dict[str, Any] | None,
    before: datetime,
) -> str:
    if before.tzinfo is None or before.utcoffset() is None:
        raise ValueError("chart cursor boundary must be timezone-aware")
    payload = json.dumps(
        {
            "v": 1,
            "instrument": instrument.symbol,
            "source": source_id,
            "period": period_id,
            "schedule": _schedule_cursor_version(schedule),
            "before": before.astimezone(UTC).isoformat(),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_chart_page_cursor(
    token: str,
    instrument: Instrument,
    *,
    source_id: str,
    period_id: str,
    schedule: dict[str, Any] | None,
) -> datetime:
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.b64decode(token + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError
        expected = (
            instrument.symbol,
            source_id,
            period_id,
            _schedule_cursor_version(schedule),
        )
        actual = (
            payload.get("instrument"),
            payload.get("source"),
            payload.get("period"),
            payload.get("schedule"),
        )
        if actual != expected:
            raise ValueError("chart cursor belongs to another dataset")
        parsed_before = datetime.fromisoformat(str(payload["before"]))
        if parsed_before.tzinfo is None or parsed_before.utcoffset() is None:
            raise ValueError
        before = parsed_before.astimezone(UTC)
    except ValueError as error:
        if str(error) == "chart cursor belongs to another dataset":
            raise
        raise ValueError("invalid chart page cursor") from error
    except (binascii.Error, json.JSONDecodeError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError("invalid chart page cursor") from error
    return before


class _PeriodBarPages(Protocol):
    async def get_page(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: dict[str, Any] | None,
        before: datetime | None = None,
        page_size: int = 500,
    ) -> PeriodBarPage: ...

    async def materialize_page(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: dict[str, Any] | None,
        before: datetime | None = None,
        page_size: int = 500,
    ) -> PeriodBarPage: ...


class _HistoryBackfiller(Protocol):
    async def backfill(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime,
        count: int,
        revalidate: bool = False,
    ) -> BarBackfillResult: ...


class ChartHistoryLocalStatus(StrEnum):
    READY = "ready"
    EMPTY = "empty"


class ChartHistorySourceStatus(StrEnum):
    AVAILABLE = "available"
    DEFERRED = "deferred"
    EXHAUSTED = "exhausted"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ChartHistoryResult:
    source_id: str
    period_id: str
    local_status: ChartHistoryLocalStatus
    source_status: ChartHistorySourceStatus
    page: PeriodBarPage
    next_before: datetime | None
    backfill: BarBackfillResult | None = None


def chart_history_demand_minutes(period_id: str, count_back: int) -> int:
    """Plans one bounded provider transport page from a logical Bar demand."""

    if not 1 <= count_back <= 10_000:
        raise ValueError("count_back must be between 1 and 10000")
    definition = PERIOD_DEFINITIONS.get(period_id)
    if definition is None:
        raise ValueError(f"unsupported chart period {period_id!r}")
    if definition.seconds is not None:
        minutes = max(1, (definition.seconds * count_back + 59) // 60)
    elif definition.minutes is not None:
        minutes = definition.minutes * count_back
    else:
        minutes = _CALENDAR_PERIOD_MINUTES[str(definition.calendar_unit)] * count_back
    return min(_MAX_TRANSPORT_MINUTES, max(1, minutes))


class ChartHistoryCoordinator:
    """Coordinates one progressive older-history step on the server.

    The caller expresses Bars, not minute transport windows. Each call performs
    at most one bounded provider page, then re-reads the canonical period page.
    Empty checked windows advance through coverage metadata without inventing a
    provider history floor.
    """

    def __init__(
        self,
        period_bars: _PeriodBarPages,
        realtime_bars: _HistoryBackfiller,
    ) -> None:
        self._period_bars = period_bars
        self._realtime_bars = realtime_bars

    async def ensure_older(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        period_id: str,
        schedule: dict[str, Any] | None,
        before: datetime,
        count_back: int,
        backfill_supported: bool,
    ) -> ChartHistoryResult:
        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("before must be timezone-aware")
        chart_history_demand_minutes(period_id, count_back)
        before = before.astimezone(UTC)
        page = await self._period_bars.get_page(
            instrument,
            source_id=source_id,
            period_id=period_id,
            schedule=schedule,
            before=before,
            page_size=count_back,
        )
        self._require_progress(page, before)
        if page.items:
            return self._result(
                source_id,
                period_id,
                page,
                ChartHistorySourceStatus.AVAILABLE,
            )

        if period_id in {"timeline", "1s"} or not backfill_supported:
            return self._result(
                source_id,
                period_id,
                page,
                ChartHistorySourceStatus.UNSUPPORTED,
            )

        count = chart_history_demand_minutes(period_id, count_back)
        end = before.replace(second=0, microsecond=0)
        start = end - timedelta(minutes=count)
        backfill = await self._realtime_bars.backfill(
            instrument,
            source_id=source_id,
            start=start,
            count=count,
        )
        page = await self._period_bars.materialize_page(
            instrument,
            source_id=source_id,
            period_id=period_id,
            schedule=schedule,
            before=before,
            page_size=count_back,
        )
        self._require_progress(page, before)
        next_before = page.next_before or self._confirmed_resume_before(backfill, end)
        return ChartHistoryResult(
            source_id=source_id,
            period_id=period_id,
            local_status=(
                ChartHistoryLocalStatus.READY if page.items else ChartHistoryLocalStatus.EMPTY
            ),
            source_status=self._source_status(backfill.state),
            page=page,
            next_before=next_before,
            backfill=backfill,
        )

    @staticmethod
    def _result(
        source_id: str,
        period_id: str,
        page: PeriodBarPage,
        source_status: ChartHistorySourceStatus,
    ) -> ChartHistoryResult:
        return ChartHistoryResult(
            source_id=source_id,
            period_id=period_id,
            local_status=(
                ChartHistoryLocalStatus.READY if page.items else ChartHistoryLocalStatus.EMPTY
            ),
            source_status=source_status,
            page=page,
            next_before=page.next_before,
        )

    @staticmethod
    def _source_status(state: BarBackfillState) -> ChartHistorySourceStatus:
        if state is BarBackfillState.DEFERRED:
            return ChartHistorySourceStatus.DEFERRED
        if state is BarBackfillState.EXHAUSTED:
            return ChartHistorySourceStatus.EXHAUSTED
        return ChartHistorySourceStatus.AVAILABLE

    @staticmethod
    def _confirmed_resume_before(
        backfill: BarBackfillResult,
        requested_end: datetime,
    ) -> datetime | None:
        if (
            backfill.covered_start is not None
            and backfill.covered_end is not None
            and backfill.covered_start < requested_end <= backfill.covered_end
        ):
            return backfill.covered_start
        return None

    @staticmethod
    def _require_progress(page: PeriodBarPage, before: datetime) -> None:
        if page.next_before is not None and page.next_before >= before:
            raise RuntimeError("chart history page cursor did not advance")
