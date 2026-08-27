from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import combinations
from typing import Any

from tracefang.application.period_bars import PeriodBarService
from tracefang.domain.market_events import BarState, RealtimeBar
from tracefang.domain.models import Instrument

MULTI_TIMEFRAME_CONTRACT_VERSION = "multi-timeframe-trend-v1"
MULTI_TIMEFRAME_PROFILE_ID = "swing-1h-1d-1w-v1"
_PAGE_SIZE = 32
_MAX_PAGES_PER_TIMEFRAME = 10


class TimeframeHorizon(StrEnum):
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class TimeframeSummaryState(StrEnum):
    READY = "ready"
    INSUFFICIENT_DATA = "insufficient_data"
    UNAVAILABLE = "unavailable"


class TrendDirection(StrEnum):
    UP = "up"
    DOWN = "down"
    MIXED = "mixed"
    UNAVAILABLE = "unavailable"


class TimeframeComparisonState(StrEnum):
    ALIGNED = "aligned"
    DIVERGENT = "divergent"
    MIXED = "mixed"
    NOT_COMPARABLE = "not_comparable"


@dataclass(frozen=True, slots=True)
class TimeframeTrendSpec:
    horizon: TimeframeHorizon
    period_id: str
    required_final_bars: int = 20
    fast_sma_bars: int = 5
    slow_sma_bars: int = 20


MULTI_TIMEFRAME_SPECS = (
    TimeframeTrendSpec(TimeframeHorizon.SHORT, "1h"),
    TimeframeTrendSpec(TimeframeHorizon.MEDIUM, "1d"),
    TimeframeTrendSpec(TimeframeHorizon.LONG, "1w"),
)


@dataclass(frozen=True, slots=True)
class TimeframeTrendSummary:
    horizon: TimeframeHorizon
    period_id: str
    state: TimeframeSummaryState
    direction: TrendDirection
    required_final_bars: int
    loaded_bar_count: int
    eligible_final_bar_count: int
    used_bar_count: int
    excluded_non_final_bars: int
    excluded_after_as_of_bars: int
    excluded_invalid_time_bars: int
    first_open_time: datetime | None
    last_open_time: datetime | None
    last_bucket_end: datetime | None
    last_available_at: datetime | None
    last_close: Decimal | None
    sma_fast: Decimal | None
    sma_slow: Decimal | None
    window_return_percent: Decimal | None
    limitation: str | None


@dataclass(frozen=True, slots=True)
class TimeframeDifference:
    left: TimeframeHorizon
    right: TimeframeHorizon
    left_direction: TrendDirection
    right_direction: TrendDirection


@dataclass(frozen=True, slots=True)
class TimeframeComparison:
    state: TimeframeComparisonState
    comparable: bool
    aligned_direction: TrendDirection | None
    differences: tuple[TimeframeDifference, ...]
    incomparable_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MultiTimeframeTrendContext:
    instrument_symbol: str
    source_id: str
    decision_as_of: datetime
    state: str
    timeframes: tuple[TimeframeTrendSummary, ...]
    comparison: TimeframeComparison


@dataclass(frozen=True, slots=True)
class _CausalBars:
    values: tuple[RealtimeBar, ...]
    excluded_non_final: int
    excluded_after_as_of: int
    excluded_invalid_time: int


def _aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _bucket_end(value: RealtimeBar) -> datetime | None:
    raw = (value.source.raw_payload or {}).get("bucket_end")
    if raw is None:
        return value.open_time.astimezone(UTC) + value.interval
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    parsed = parsed.astimezone(UTC)
    return parsed if parsed > value.open_time.astimezone(UTC) else None


def _causal_bars(
    values: Sequence[RealtimeBar],
    *,
    decision_as_of: datetime,
) -> _CausalBars:
    cutoff = _aware_utc(decision_as_of, "decision_as_of")
    by_open_time: dict[datetime, RealtimeBar] = {}
    for value in values:
        current = by_open_time.get(value.open_time)
        if current is None or (value.revision, value.source.received_at) > (
            current.revision,
            current.source.received_at,
        ):
            by_open_time[value.open_time] = value

    eligible: list[RealtimeBar] = []
    excluded_non_final = 0
    excluded_after_as_of = 0
    excluded_invalid_time = 0
    for value in sorted(by_open_time.values(), key=lambda item: item.open_time):
        if value.state is not BarState.FINAL or value.finalized_at is None:
            excluded_non_final += 1
            continue
        bucket_end = _bucket_end(value)
        if bucket_end is None:
            excluded_invalid_time += 1
            continue
        availability_times = (
            bucket_end,
            value.finalized_at.astimezone(UTC),
            value.source.observed_at.astimezone(UTC),
            value.source.received_at.astimezone(UTC),
        )
        if any(item > cutoff for item in availability_times):
            excluded_after_as_of += 1
            continue
        eligible.append(value)
    return _CausalBars(
        values=tuple(eligible),
        excluded_non_final=excluded_non_final,
        excluded_after_as_of=excluded_after_as_of,
        excluded_invalid_time=excluded_invalid_time,
    )


def _average(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _summary(
    spec: TimeframeTrendSpec,
    values: Sequence[RealtimeBar],
    *,
    decision_as_of: datetime,
    scan_limit_reached: bool,
) -> TimeframeTrendSummary:
    causal = _causal_bars(values, decision_as_of=decision_as_of)
    available = causal.values
    window = available[-spec.required_final_bars :]
    closes = tuple(item.close for item in window)
    fast_sma = (
        _average(closes[-spec.fast_sma_bars :])
        if len(closes) >= spec.fast_sma_bars
        and all(item > 0 for item in closes[-spec.fast_sma_bars :])
        else None
    )
    sample_complete = len(window) >= spec.required_final_bars
    prices_valid = all(item > 0 for item in closes)
    ready = sample_complete and prices_valid
    slow_sma = _average(closes[-spec.slow_sma_bars :]) if ready else None
    window_return = ((closes[-1] / closes[0]) - Decimal(1)) * Decimal(100) if ready else None
    direction = TrendDirection.UNAVAILABLE
    if ready and fast_sma is not None and slow_sma is not None and window_return is not None:
        if closes[-1] > fast_sma > slow_sma and window_return > 0:
            direction = TrendDirection.UP
        elif closes[-1] < fast_sma < slow_sma and window_return < 0:
            direction = TrendDirection.DOWN
        else:
            direction = TrendDirection.MIXED

    latest = window[-1] if window else None
    latest_bucket_end = _bucket_end(latest) if latest is not None else None
    latest_available_at = None
    if latest is not None and latest.finalized_at is not None and latest_bucket_end is not None:
        latest_available_at = max(
            latest_bucket_end,
            latest.finalized_at,
            latest.source.observed_at,
            latest.source.received_at,
        )
    limitation = None
    if sample_complete and not prices_valid:
        limitation = "non_positive_close_not_comparable"
    elif not ready:
        limitation = (
            "history_scan_limit_before_required_sample"
            if scan_limit_reached
            else f"requires_{spec.required_final_bars}_final_bars_has_{len(window)}"
        )
    return TimeframeTrendSummary(
        horizon=spec.horizon,
        period_id=spec.period_id,
        state=(
            TimeframeSummaryState.READY
            if ready
            else (
                TimeframeSummaryState.UNAVAILABLE
                if sample_complete
                else TimeframeSummaryState.INSUFFICIENT_DATA
            )
        ),
        direction=direction,
        required_final_bars=spec.required_final_bars,
        loaded_bar_count=len(values),
        eligible_final_bar_count=len(available),
        used_bar_count=len(window),
        excluded_non_final_bars=causal.excluded_non_final,
        excluded_after_as_of_bars=causal.excluded_after_as_of,
        excluded_invalid_time_bars=causal.excluded_invalid_time,
        first_open_time=window[0].open_time if window else None,
        last_open_time=latest.open_time if latest is not None else None,
        last_bucket_end=latest_bucket_end,
        last_available_at=latest_available_at,
        last_close=latest.close if latest is not None else None,
        sma_fast=fast_sma,
        sma_slow=slow_sma,
        window_return_percent=window_return,
        limitation=limitation,
    )


def _comparison(values: Sequence[TimeframeTrendSummary]) -> TimeframeComparison:
    not_ready = tuple(item for item in values if item.state is not TimeframeSummaryState.READY)
    if not_ready:
        return TimeframeComparison(
            state=TimeframeComparisonState.NOT_COMPARABLE,
            comparable=False,
            aligned_direction=None,
            differences=(),
            incomparable_reasons=tuple(
                f"{item.horizon.value}:{item.limitation or 'unavailable'}" for item in not_ready
            ),
        )

    differences = tuple(
        TimeframeDifference(
            left=left.horizon,
            right=right.horizon,
            left_direction=left.direction,
            right_direction=right.direction,
        )
        for left, right in combinations(values, 2)
        if left.direction is not right.direction
    )
    directions = {item.direction for item in values}
    if len(directions) == 1 and next(iter(directions)) in {
        TrendDirection.UP,
        TrendDirection.DOWN,
    }:
        aligned = next(iter(directions))
        state = TimeframeComparisonState.ALIGNED
    elif TrendDirection.UP in directions and TrendDirection.DOWN in directions:
        aligned = None
        state = TimeframeComparisonState.DIVERGENT
    else:
        aligned = None
        state = TimeframeComparisonState.MIXED
    return TimeframeComparison(
        state=state,
        comparable=True,
        aligned_direction=aligned,
        differences=differences,
        incomparable_reasons=(),
    )


class MultiTimeframeTrendService:
    def __init__(self, period_bars: PeriodBarService) -> None:
        self._period_bars = period_bars

    async def snapshot(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        schedule: Mapping[str, Any] | None,
        decision_as_of: datetime,
    ) -> MultiTimeframeTrendContext:
        cutoff = _aware_utc(decision_as_of, "decision_as_of")
        summaries: list[TimeframeTrendSummary] = []
        for spec in MULTI_TIMEFRAME_SPECS:
            values, scan_limit_reached = await self._load_values(
                instrument,
                source_id=source_id,
                schedule=schedule,
                spec=spec,
                decision_as_of=cutoff,
            )
            summaries.append(
                _summary(
                    spec,
                    values,
                    decision_as_of=cutoff,
                    scan_limit_reached=scan_limit_reached,
                )
            )
        ready_count = sum(item.state is TimeframeSummaryState.READY for item in summaries)
        if ready_count == len(summaries):
            state = "ready"
        elif ready_count:
            state = "partial"
        elif any(item.state is TimeframeSummaryState.UNAVAILABLE for item in summaries):
            state = "unavailable"
        else:
            state = "insufficient_data"
        return MultiTimeframeTrendContext(
            instrument_symbol=instrument.symbol,
            source_id=source_id,
            decision_as_of=cutoff,
            state=state,
            timeframes=tuple(summaries),
            comparison=_comparison(summaries),
        )

    async def _load_values(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        schedule: Mapping[str, Any] | None,
        spec: TimeframeTrendSpec,
        decision_as_of: datetime,
    ) -> tuple[tuple[RealtimeBar, ...], bool]:
        cursor = decision_as_of
        values: dict[datetime, RealtimeBar] = {}
        for _ in range(_MAX_PAGES_PER_TIMEFRAME):
            page = await self._period_bars.get_page(
                instrument,
                source_id=source_id,
                period_id=spec.period_id,
                schedule=schedule,
                before=cursor,
                page_size=_PAGE_SIZE,
            )
            for value in page.items:
                current = values.get(value.open_time)
                if current is None or (value.revision, value.source.received_at) > (
                    current.revision,
                    current.source.received_at,
                ):
                    values[value.open_time] = value
            loaded = tuple(sorted(values.values(), key=lambda item: item.open_time))
            causal = _causal_bars(loaded, decision_as_of=decision_as_of)
            if len(causal.values) >= spec.required_final_bars or not page.has_more:
                return loaded, False
            if page.next_before is None:
                return loaded, False
            next_cursor = _aware_utc(page.next_before, "period page cursor")
            if next_cursor >= cursor:
                raise RuntimeError("period page cursor did not move backward")
            cursor = next_cursor
        return tuple(sorted(values.values(), key=lambda item: item.open_time)), True


def multi_timeframe_payload(
    value: MultiTimeframeTrendContext,
    *,
    code: str,
) -> dict[str, Any]:
    def number(item: Decimal | None) -> float | None:
        return float(item) if item is not None else None

    return {
        "contract_version": MULTI_TIMEFRAME_CONTRACT_VERSION,
        "profile_id": MULTI_TIMEFRAME_PROFILE_ID,
        "state": value.state,
        "code": code,
        "instrument_symbol": value.instrument_symbol,
        "source_id": value.source_id,
        "decision_as_of": value.decision_as_of.isoformat(),
        "as_of_policy": (
            "state_final_and_bucket_end_finalized_observed_received_lte_decision_as_of"
        ),
        "direction_rule": (
            "up_if_close_gt_sma5_gt_sma20_and_return20_gt_0;"
            "down_if_close_lt_sma5_lt_sma20_and_return20_lt_0;otherwise_mixed"
        ),
        "period_mapping": {
            spec.horizon.value: {
                "period_id": spec.period_id,
                "required_final_bars": spec.required_final_bars,
                "fast_sma_bars": spec.fast_sma_bars,
                "slow_sma_bars": spec.slow_sma_bars,
            }
            for spec in MULTI_TIMEFRAME_SPECS
        },
        "timeframes": [
            {
                "horizon": item.horizon.value,
                "period_id": item.period_id,
                "state": item.state.value,
                "direction": item.direction.value,
                "required_final_bars": item.required_final_bars,
                "loaded_bar_count": item.loaded_bar_count,
                "eligible_final_bar_count": item.eligible_final_bar_count,
                "used_bar_count": item.used_bar_count,
                "excluded_non_final_bars": item.excluded_non_final_bars,
                "excluded_after_as_of_bars": item.excluded_after_as_of_bars,
                "excluded_invalid_time_bars": item.excluded_invalid_time_bars,
                "first_open_time": (
                    item.first_open_time.isoformat() if item.first_open_time else None
                ),
                "last_open_time": (
                    item.last_open_time.isoformat() if item.last_open_time else None
                ),
                "last_bucket_end": (
                    item.last_bucket_end.isoformat() if item.last_bucket_end else None
                ),
                "last_available_at": (
                    item.last_available_at.isoformat() if item.last_available_at else None
                ),
                "last_close": number(item.last_close),
                "sma_fast": number(item.sma_fast),
                "sma_slow": number(item.sma_slow),
                "window_return_percent": number(item.window_return_percent),
                "limitation": item.limitation,
            }
            for item in value.timeframes
        ],
        "comparison": {
            "state": value.comparison.state.value,
            "comparable": value.comparison.comparable,
            "aligned_direction": (
                value.comparison.aligned_direction.value
                if value.comparison.aligned_direction is not None
                else None
            ),
            "differences": [
                {
                    "left": item.left.value,
                    "right": item.right.value,
                    "left_direction": item.left_direction.value,
                    "right_direction": item.right_direction.value,
                }
                for item in value.comparison.differences
            ],
            "incomparable_reasons": list(value.comparison.incomparable_reasons),
        },
        "limitations": [
            "Each horizon uses only closed Bars from the same instrument and source.",
            (
                "The latest closed Bars naturally end at different times; decision_as_of is "
                "the common information cutoff, not a claim that bucket ends are identical."
            ),
            (
                "Fewer than 20 eligible Bars makes that horizon insufficient and the "
                "cross-horizon comparison not comparable."
            ),
            (
                "Historical revision snapshots are not retained; a later revision received "
                "after decision_as_of is conservatively excluded rather than reconstructed."
            ),
            (
                "Trend direction is a deterministic 5/20 SMA and 20-Bar return summary, not "
                "a probability or trading recommendation."
            ),
        ],
    }
