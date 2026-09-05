from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from tracefang.application.quotes import QuoteView
from tracefang.domain.errors import ProviderAuthenticationError, ProviderUnavailableError
from tracefang.domain.market_events import (
    BarEvent,
    BarFinalityPolicy,
    BarState,
    MarketEvent,
    QuoteEvent,
    QuoteSample,
    RealtimeBar,
    quote_event_id,
    quote_observation_kind,
)
from tracefang.domain.models import (
    Candle,
    Instrument,
    QuoteSnapshot,
    SourceMetadata,
)

REALTIME_BAR_READ_PAGE_SIZE_MAX = 10_000


class SourceBarStore(Protocol):
    async def load_quote_event_page(
        self,
        instrument: Instrument,
        *,
        source_ids: tuple[str, ...],
        before_id: int | None = None,
        page_size: int = 2_000,
    ) -> tuple[QuoteSample, ...]: ...

    async def load_realtime_bars(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[RealtimeBar, ...]: ...

    async def save_realtime_bars(self, bars: Sequence[RealtimeBar]) -> None: ...

    async def load_realtime_bars_before(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        before: datetime | None = None,
        count: int = 2_000,
    ) -> tuple[RealtimeBar, ...]: ...

    async def load_source_candles(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]: ...

    async def load_source_candles_before(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        before: datetime | None = None,
        count: int = 2_000,
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

    async def load_quote_candles_before(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        before: datetime | None = None,
        count: int = 2_000,
    ) -> tuple[Candle, ...]: ...

    async def save_candles(self, candles: Sequence[Candle]) -> None: ...

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

    async def load_realtime_bar_series_state(
        self,
        instrument: Instrument,
        *,
        realtime_source_id: str,
        interval: timedelta = timedelta(minutes=1),
    ) -> RealtimeBarSeriesState | None: ...

    async def commit_historical_bar_batch(
        self,
        instrument: Instrument,
        *,
        realtime_source_id: str,
        upstream_channel_id: str,
        provider_symbol: str,
        batch: HistoricalBarBatch,
        bars: Sequence[RealtimeBar],
    ) -> RealtimeBarSeriesState: ...


class HistoricalBarProvider(Protocol):
    name: str

    def provider_symbol(self, instrument: Instrument) -> str: ...

    async def fetch_historical_candles(
        self,
        instrument: Instrument,
        *,
        start: datetime,
        count: int,
    ) -> HistoricalBarBatch: ...


class RealtimeBarWriter(Protocol):
    def submit_realtime_bars(self, bars: Sequence[RealtimeBar]) -> bool: ...


@dataclass(frozen=True, slots=True)
class RealtimeBarContract:
    """Data-driven Bar delivery contract of one complete realtime source."""

    source_id: str
    authoritative_bar_channel_id: str
    quote_channel_ids: tuple[str, ...]
    history_provider: HistoricalBarProvider | None = None
    interval: timedelta = timedelta(minutes=1)
    quote_projection_intervals: tuple[timedelta, ...] = (
        timedelta(seconds=1),
        timedelta(minutes=1),
    )
    finality_policy: BarFinalityPolicy = BarFinalityPolicy.NEXT_AUTHORITATIVE_BAR

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("source_id cannot be empty")
        if not self.authoritative_bar_channel_id.strip():
            raise ValueError("authoritative_bar_channel_id cannot be empty")
        if not self.quote_channel_ids:
            raise ValueError("a realtime Bar contract requires at least one quote channel")
        if len(set(self.quote_channel_ids)) != len(self.quote_channel_ids):
            raise ValueError("realtime Bar quote channels must be unique")
        if self.interval <= timedelta(0):
            raise ValueError("interval must be positive")
        if not self.quote_projection_intervals:
            raise ValueError("a realtime Bar contract requires quote projection intervals")
        if len(set(self.quote_projection_intervals)) != len(self.quote_projection_intervals):
            raise ValueError("quote projection intervals must be unique")
        if any(interval <= timedelta(0) for interval in self.quote_projection_intervals):
            raise ValueError("quote projection intervals must be positive")
        if self.interval not in self.quote_projection_intervals:
            raise ValueError("the authoritative interval must be quote-projected")


@dataclass(frozen=True, slots=True)
class HistoricalBarBatch:
    """Validated evidence returned by any same-source historical provider."""

    candles: tuple[Candle, ...]
    checked_start: datetime
    checked_end: datetime
    authoritative_through: datetime
    evidence_version: str
    checked_at: datetime
    history_floor: datetime | None = None
    interval: timedelta = timedelta(minutes=1)

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError("historical batch interval must be positive")
        for value, field_name in (
            (self.checked_start, "checked_start"),
            (self.checked_end, "checked_end"),
            (self.authoritative_through, "authoritative_through"),
            (self.checked_at, "checked_at"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"historical batch {field_name} must be timezone-aware")
        if self.history_floor is not None and (
            self.history_floor.tzinfo is None or self.history_floor.utcoffset() is None
        ):
            raise ValueError("historical batch history_floor must be timezone-aware")
        if self.checked_end <= self.checked_start:
            raise ValueError("historical batch checked range must be positive")
        if self.authoritative_through > self.checked_end:
            raise ValueError("historical batch authority is outside the checked range")
        if self.history_floor is not None and self.history_floor > self.authoritative_through:
            raise ValueError("historical batch history_floor exceeds authority")
        for value, field_name in (
            (self.checked_start, "checked_start"),
            (self.checked_end, "checked_end"),
            (self.authoritative_through, "authoritative_through"),
            (self.history_floor, "history_floor"),
        ):
            if value is not None and not self._is_aligned(value):
                raise ValueError(f"historical batch {field_name} must be interval-aligned")
        if not self.evidence_version.strip():
            raise ValueError("historical batch evidence_version cannot be empty")
        seen: set[datetime] = set()
        for candle in self.candles:
            if candle.interval != self.interval:
                raise ValueError("historical batch candle interval does not match")
            if not self.checked_start <= candle.open_time < self.checked_end:
                raise ValueError("historical batch candle is outside the checked range")
            if candle.open_time in seen:
                raise ValueError("historical batch contains duplicate open times")
            seen.add(candle.open_time)

    def _is_aligned(self, value: datetime) -> bool:
        interval_microseconds = int(self.interval / timedelta(microseconds=1))
        timestamp_microseconds = int(value.timestamp() * 1_000_000)
        return timestamp_microseconds % interval_microseconds == 0


@dataclass(frozen=True, slots=True)
class RealtimeBarSeriesState:
    realtime_source_id: str
    instrument_symbol: str
    upstream_channel_id: str
    provider_symbol: str
    interval: timedelta
    latest_authoritative_open_time: datetime | None
    authoritative_through: datetime
    history_floor: datetime | None
    tail_checked_through: datetime | None
    tail_checked_at: datetime | None
    evidence_version: str
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError("series state interval must be positive")
        if not self.realtime_source_id or not self.instrument_symbol:
            raise ValueError("series state identity cannot be empty")
        if not self.upstream_channel_id or not self.provider_symbol:
            raise ValueError("series state upstream identity cannot be empty")
        if not self.evidence_version:
            raise ValueError("series state evidence version cannot be empty")
        for value, field_name in (
            (self.latest_authoritative_open_time, "latest_authoritative_open_time"),
            (self.authoritative_through, "authoritative_through"),
            (self.history_floor, "history_floor"),
            (self.tail_checked_through, "tail_checked_through"),
            (self.tail_checked_at, "tail_checked_at"),
            (self.updated_at, "updated_at"),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"series state {field_name} must be timezone-aware")
        if (
            self.latest_authoritative_open_time is not None
            and self.latest_authoritative_open_time >= self.authoritative_through
        ):
            raise ValueError("latest authoritative Bar must precede authority boundary")
        if self.history_floor is not None and self.history_floor > self.authoritative_through:
            raise ValueError("history floor cannot exceed authority boundary")


class BarBackfillState(StrEnum):
    CACHED = "cached"
    JOINED = "joined"
    FETCHED = "fetched"
    ADVANCED = "advanced"
    EXHAUSTED = "exhausted"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class BarBackfillResult:
    source_id: str
    state: BarBackfillState
    start: datetime
    end: datetime
    row_count: int
    covered_start: datetime | None = None
    covered_end: datetime | None = None
    authoritative_through: datetime | None = None
    history_floor: datetime | None = None
    retry_after: datetime | None = None
    evidence_version: str | None = None


@dataclass(frozen=True, slots=True)
class BarBackfillMetrics:
    cache_hits: int
    upstream_calls: int
    joined_calls: int
    written_rows: int
    failures: int
    pending: int
    last_failure_at: datetime | None
    last_failure_type: str | None


@dataclass(frozen=True, slots=True)
class _BackfillFailure:
    count: int
    retry_after: datetime


@dataclass(frozen=True, slots=True)
class QuoteSamplePage:
    items: tuple[QuoteSample, ...]
    next_cursor: int | None
    has_more: bool


_BarKey = tuple[str, Instrument, timedelta, datetime]
_SeriesKey = tuple[str, Instrument, timedelta]


class RealtimeBarService:
    """Reduces every source's quote and Bar events through one lifecycle state machine."""

    def __init__(
        self,
        store: SourceBarStore | None,
        *,
        contracts: tuple[RealtimeBarContract, ...],
        writer: RealtimeBarWriter | None = None,
        history_concurrency: int = 2,
        tail_cooldown: timedelta = timedelta(seconds=5),
        revalidate_cooldown: timedelta = timedelta(seconds=30),
        backoff_base: timedelta = timedelta(seconds=1),
        backoff_max: timedelta = timedelta(minutes=1),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not contracts:
            raise ValueError("at least one realtime Bar contract is required")
        self._store = store
        self._writer = writer
        self._contracts = {item.source_id: item for item in contracts}
        if len(self._contracts) != len(contracts):
            raise ValueError("realtime Bar source ids must be unique")
        if history_concurrency < 1:
            raise ValueError("history_concurrency must be positive")
        if min(tail_cooldown, revalidate_cooldown, backoff_base, backoff_max) <= timedelta(0):
            raise ValueError("history cooldowns and backoff must be positive")
        if backoff_max < backoff_base:
            raise ValueError("backoff_max must be at least backoff_base")

        self._source_by_quote_channel: dict[str, str] = {}
        self._source_by_bar_channel: dict[str, str] = {}
        for contract in contracts:
            self._bind_channels(
                contract.source_id,
                contract.quote_channel_ids,
                self._source_by_quote_channel,
                kind="quote",
            )
            self._bind_channels(
                contract.source_id,
                (contract.authoritative_bar_channel_id,),
                self._source_by_bar_channel,
                kind="Bar",
            )

        self._bars: dict[_BarKey, RealtimeBar] = {}
        self._watermarks: dict[_SeriesKey, datetime] = {}
        self._series_states: dict[_SeriesKey, RealtimeBarSeriesState] = {}
        self._backfills: dict[
            tuple[str, Instrument, datetime, int, bool],
            asyncio.Task[BarBackfillResult],
        ] = {}
        self._backfill_locks: dict[tuple[str, Instrument], asyncio.Lock] = {}
        self._history_semaphore = asyncio.Semaphore(history_concurrency)
        self._tail_cooldown = tail_cooldown
        self._revalidate_cooldown = revalidate_cooldown
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._clock = clock or (lambda: datetime.now(UTC))
        self._backfill_failures: dict[tuple[str, Instrument], _BackfillFailure] = {}
        self._revalidation_checks: dict[
            tuple[str, Instrument, datetime, datetime, str | None],
            datetime,
        ] = {}
        self._authoritative_watermarks: dict[_SeriesKey, datetime] = {}
        self._backfill_metric_counts = {
            "cache_hits": 0,
            "upstream_calls": 0,
            "joined_calls": 0,
            "written_rows": 0,
            "failures": 0,
        }
        self._last_backfill_failure_at: datetime | None = None
        self._last_backfill_failure_type: str | None = None

    @staticmethod
    def _bind_channels(
        source_id: str,
        channel_ids: tuple[str, ...],
        target: dict[str, str],
        *,
        kind: str,
    ) -> None:
        for channel_id in channel_ids:
            owner = target.get(channel_id)
            if owner is not None and owner != source_id:
                raise ValueError(
                    f"{kind} channel {channel_id!r} belongs to multiple realtime sources"
                )
            target[channel_id] = source_id

    def normalize_quote(self, quote: QuoteSnapshot) -> QuoteEvent | None:
        source_id = self._source_by_quote_channel.get(quote.source.provider)
        if source_id is None:
            return None
        return QuoteEvent(
            source_id=source_id,
            channel_id=quote.source.provider,
            quote=quote,
            sequence=self._sequence(quote.source),
        )

    def normalize_bar(self, candle: Candle) -> BarEvent | None:
        source_id = self._source_by_bar_channel.get(candle.source.provider)
        if source_id is None:
            return None
        state = self._state_hint(candle.source)
        return BarEvent(
            source_id=source_id,
            channel_id=candle.source.provider,
            candle=candle,
            state=state,
            sequence=self._sequence(candle.source),
            finalized_at=(candle.source.received_at if state is BarState.FINAL else None),
        )

    def apply(self, event: MarketEvent) -> tuple[RealtimeBar, ...]:
        """Reduces one event and returns complete Bar upserts in delivery order."""

        transitions = self._coalesce(self._apply(event))
        if transitions and self._writer is not None:
            self._writer.submit_realtime_bars(transitions)
        return transitions

    def accept(self, event: MarketEvent) -> bool:
        return bool(self.apply(event))

    def accept_quote(self, quote: QuoteSnapshot) -> bool:
        event = self.normalize_quote(quote)
        return self.accept(event) if event is not None else False

    def sample_from_quote_event(self, event: QuoteEvent) -> QuoteSample:
        if event.source_id not in self._contracts:
            raise ProviderUnavailableError(
                f"{event.source_id} has no source-bound timeline capability"
            )
        value = event.quote
        return QuoteSample(
            source_id=event.source_id,
            channel_id=event.channel_id,
            event_id=quote_event_id(value),
            instrument=value.instrument,
            provider_symbol=value.source.provider_symbol,
            observed_at=value.source.observed_at,
            received_at=value.source.received_at,
            value=value.last,
            observation_kind=quote_observation_kind(value),
        )

    async def get_quote_sample_page(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        before_id: int | None = None,
        page_size: int = 2_000,
    ) -> QuoteSamplePage:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        contract = self._contract(source_id)
        if self._store is None:
            return QuoteSamplePage((), None, False)
        rows = await self._store.load_quote_event_page(
            instrument,
            source_ids=contract.quote_channel_ids,
            before_id=before_id,
            page_size=page_size + 1,
        )
        has_more = len(rows) > page_size
        visible = rows[-page_size:] if has_more else rows
        items = tuple(replace(row, source_id=source_id) for row in visible)
        next_cursor = min((item.storage_id for item in visible if item.storage_id), default=None)
        return QuoteSamplePage(items, next_cursor, has_more)

    def accept_bar(self, candle: Candle) -> bool:
        event = self.normalize_bar(candle)
        return self.accept(event) if event is not None else False

    def accept_view(self, view: QuoteView) -> bool:
        """Adapts an application-derived quote without adding source-specific Bar logic."""

        return bool(self.apply_view(view))

    def apply_view(self, view: QuoteView) -> tuple[RealtimeBar, ...]:
        """Returns complete Bar upserts for one application-derived quote view."""

        if view.source_id not in self._contracts:
            return ()
        value = view.quote
        quote = QuoteSnapshot(
            instrument=value.instrument,
            last=value.last,
            open=value.open,
            high=value.high,
            low=value.low,
            volume=value.volume,
            change=value.change,
            change_percent=value.change_percent,
            source=value.source,
        )
        return self.apply(
            QuoteEvent(
                source_id=view.source_id,
                channel_id=value.source.provider,
                quote=quote,
                sequence=self._sequence(value.source),
            )
        )

    def _apply(self, event: MarketEvent) -> list[RealtimeBar]:
        if isinstance(event, QuoteEvent):
            return self._apply_quote(event)
        return self._apply_bar(event)

    def _apply_quote(self, event: QuoteEvent) -> list[RealtimeBar]:
        contract = self._contract(event.source_id)
        if (
            event.channel_id not in contract.quote_channel_ids
            and event.channel_id != event.source_id
        ):
            raise ValueError("quote event channel is not part of the realtime Bar contract")
        transitions: list[RealtimeBar] = []
        for interval in contract.quote_projection_intervals:
            transitions.extend(self._apply_quote_interval(event, interval=interval))
        return transitions

    def _apply_quote_interval(
        self,
        event: QuoteEvent,
        *,
        interval: timedelta,
    ) -> list[RealtimeBar]:
        quote = event.quote
        open_time = self._floor_time(quote.source.observed_at, interval)
        series_key = (event.source_id, quote.instrument, interval)
        watermark = self._watermarks.get(series_key)
        if watermark is not None and open_time < watermark:
            return []

        transitions: list[RealtimeBar] = []
        if watermark is None or open_time > watermark:
            if interval == timedelta(seconds=1):
                transitions.extend(
                    self._finalize_quote_before(
                        event.source_id,
                        quote.instrument,
                        interval,
                        open_time,
                        finalized_at=quote.source.received_at,
                    )
                )
            self._watermarks[series_key] = open_time

        key = (*series_key, open_time)
        current = self._bars.get(key)
        if current is not None and current.state is BarState.FINAL:
            return []
        if current is not None and quote.source.received_at < current.source.received_at:
            return []

        if current is None:
            value = RealtimeBar(
                instrument=quote.instrument,
                interval=interval,
                open_time=open_time,
                open=quote.last,
                high=quote.last,
                low=quote.last,
                close=quote.last,
                volume=None,
                source=self._public_metadata(
                    event.source_id,
                    event.channel_id,
                    quote.source,
                    derivation="quote_event",
                ),
                evidence_channel_id=event.channel_id,
                state=BarState.PROVISIONAL_QUOTE,
            )
        else:
            high = max(current.high, quote.last)
            low = min(current.low, quote.last)
            authoritative = current.state is BarState.PROVISIONAL_AUTHORITATIVE
            value = replace(
                current,
                high=high,
                low=low,
                close=quote.last,
                source=self._public_metadata(
                    event.source_id,
                    current.evidence_channel_id if authoritative else event.channel_id,
                    quote.source,
                    derivation=(
                        "authoritative_bar_with_quote_overlay" if authoritative else "quote_event"
                    ),
                    last_event_channel_id=event.channel_id,
                ),
                evidence_channel_id=(
                    current.evidence_channel_id if authoritative else event.channel_id
                ),
                revision=current.revision + 1,
            )
        self._bars[key] = value
        self._trim(event.source_id, quote.instrument, interval)
        transitions.append(value)
        return transitions

    def _finalize_quote_before(
        self,
        source_id: str,
        instrument: Instrument,
        interval: timedelta,
        before: datetime,
        *,
        finalized_at: datetime,
    ) -> list[RealtimeBar]:
        values: list[RealtimeBar] = []
        for key, current in tuple(self._bars.items()):
            candidate_source, candidate_instrument, candidate_interval, open_time = key
            if (
                candidate_source != source_id
                or candidate_instrument != instrument
                or candidate_interval != interval
                or open_time >= before
                or current.state is not BarState.PROVISIONAL_QUOTE
            ):
                continue
            value = replace(
                current,
                state=BarState.FINAL,
                revision=current.revision + 1,
                finalized_at=finalized_at,
            )
            self._bars[key] = value
            values.append(value)
        return values

    def _apply_bar(self, event: BarEvent) -> list[RealtimeBar]:
        contract = self._contract(event.source_id)
        if event.channel_id != contract.authoritative_bar_channel_id:
            raise ValueError("Bar event channel is not authoritative for this realtime source")
        candle = event.candle
        if candle.interval != contract.interval:
            raise ValueError("Bar event interval does not match the realtime Bar contract")

        series_key = (event.source_id, candle.instrument, candle.interval)
        authority_boundary = (
            candle.open_time + candle.interval
            if event.state is BarState.FINAL
            else candle.open_time
        )
        current_authority = self._authoritative_watermarks.get(series_key)
        if current_authority is None or authority_boundary > current_authority:
            self._authoritative_watermarks[series_key] = authority_boundary
        transitions: list[RealtimeBar] = []
        state = event.state
        if contract.finality_policy is BarFinalityPolicy.NEXT_AUTHORITATIVE_BAR:
            watermark = self._watermarks.get(series_key)
            if watermark is not None and candle.open_time < watermark:
                state = BarState.FINAL
            elif watermark is None or candle.open_time > watermark:
                self._watermarks[series_key] = candle.open_time
                transitions.extend(
                    self._finalize_before(
                        event.source_id,
                        candle.instrument,
                        candle.interval,
                        candle.open_time,
                        finalized_at=candle.source.received_at,
                    )
                )

        key = (*series_key, candle.open_time)
        current = self._bars.get(key)
        if current is not None and candle.source.received_at < current.source.received_at:
            return transitions
        if current is not None and current.state is BarState.FINAL:
            state = BarState.FINAL

        finalized_at = None
        if state is BarState.FINAL:
            finalized_at = event.finalized_at or candle.source.received_at
        candidate = RealtimeBar(
            instrument=candle.instrument,
            interval=candle.interval,
            open_time=candle.open_time,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            source=self._public_metadata(
                event.source_id,
                event.channel_id,
                candle.source,
                derivation=(
                    "authoritative_history" if state is BarState.FINAL else "authoritative_bar"
                ),
            ),
            evidence_channel_id=event.channel_id,
            state=state,
            revision=(current.revision + 1 if current is not None else 1),
            finalized_at=finalized_at,
        )
        if current is not None and self._same_projection(current, candidate):
            return transitions
        self._bars[key] = candidate
        self._advance_live_series_authority(candidate)
        transitions.append(candidate)
        self._trim(event.source_id, candle.instrument, candle.interval)
        return transitions

    def _finalize_before(
        self,
        source_id: str,
        instrument: Instrument,
        interval: timedelta,
        before: datetime,
        *,
        finalized_at: datetime,
    ) -> list[RealtimeBar]:
        values: list[RealtimeBar] = []
        for key, current in tuple(self._bars.items()):
            candidate_source, candidate_instrument, candidate_interval, open_time = key
            if (
                candidate_source != source_id
                or candidate_instrument != instrument
                or candidate_interval != interval
                or open_time >= before
                or current.state is not BarState.PROVISIONAL_AUTHORITATIVE
            ):
                continue
            value = replace(
                current,
                state=BarState.FINAL,
                revision=current.revision + 1,
                finalized_at=finalized_at,
            )
            self._bars[key] = value
            self._advance_live_series_authority(value)
            values.append(value)
        return values

    def _advance_live_series_authority(self, bar: RealtimeBar) -> None:
        if bar.state is not BarState.FINAL:
            return
        derivation = (bar.source.raw_payload or {}).get("derivation")
        if not isinstance(derivation, str) or not derivation.startswith("authoritative_"):
            return
        series_key = (bar.source.provider, bar.instrument, bar.interval)
        boundary = bar.open_time + bar.interval
        current = self._series_states.get(series_key)
        if current is None:
            state = RealtimeBarSeriesState(
                realtime_source_id=bar.source.provider,
                instrument_symbol=bar.instrument.symbol,
                upstream_channel_id=bar.evidence_channel_id,
                provider_symbol=bar.source.provider_symbol,
                interval=bar.interval,
                latest_authoritative_open_time=bar.open_time,
                authoritative_through=boundary,
                history_floor=None,
                tail_checked_through=None,
                tail_checked_at=None,
                evidence_version=f"live:{bar.evidence_channel_id}",
                updated_at=bar.source.received_at,
            )
        else:
            authority_advanced = boundary >= current.authoritative_through
            authoritative_through = max(current.authoritative_through, boundary)
            latest_authoritative_open_time = max(
                value
                for value in (current.latest_authoritative_open_time, bar.open_time)
                if value is not None
            )
            clears_tail = (
                current.tail_checked_through is not None
                and authoritative_through >= current.tail_checked_through
            )
            state = replace(
                current,
                upstream_channel_id=(
                    bar.evidence_channel_id
                    if authority_advanced
                    else current.upstream_channel_id
                ),
                provider_symbol=(
                    bar.source.provider_symbol if authority_advanced else current.provider_symbol
                ),
                latest_authoritative_open_time=latest_authoritative_open_time,
                authoritative_through=authoritative_through,
                tail_checked_through=(None if clears_tail else current.tail_checked_through),
                tail_checked_at=(None if clears_tail else current.tail_checked_at),
                evidence_version=(
                    f"live:{bar.evidence_channel_id}"
                    if current.evidence_version.startswith("live:")
                    else current.evidence_version
                ),
                updated_at=max(current.updated_at, bar.source.received_at),
            )
        self._series_states[series_key] = state
        self._authoritative_watermarks[series_key] = max(
            self._authoritative_watermarks.get(series_key, boundary),
            boundary,
        )

    async def get_bars(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta | None = None,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[RealtimeBar, ...]:
        """Reads local projections and raw same-source evidence without upstream calls."""

        self._validate_window(start, count)
        contract = self._contract(source_id)
        selected_interval = self._projection_interval(contract, interval)
        rows: dict[datetime, RealtimeBar] = {}
        authoritative_times: list[datetime] = []
        if self._store is not None:
            projected = await self._store.load_realtime_bars(
                instrument,
                source_id=source_id,
                interval=selected_interval,
                start=start,
                count=count,
            )
            for value in projected:
                if value.source.provider != source_id or value.instrument != instrument:
                    raise RuntimeError("realtime Bar cache returned foreign projection")
                rows[value.open_time] = value
                if value.state is not BarState.PROVISIONAL_QUOTE:
                    authoritative_times.append(value.open_time)

            if selected_interval == contract.interval:
                raw_bars = await self._store.load_source_candles(
                    instrument,
                    source_id=contract.authoritative_bar_channel_id,
                    interval=selected_interval,
                    start=start,
                    count=count,
                )
                for candle in raw_bars:
                    if (
                        candle.instrument != instrument
                        or candle.source.provider != contract.authoritative_bar_channel_id
                    ):
                        raise RuntimeError("source-bound Bar cache returned foreign evidence")
                    value = self._projection_from_raw_bar(source_id, candle)
                    rows[candle.open_time] = self._merge_for_read(rows.get(candle.open_time), value)
                    authoritative_times.append(candle.open_time)

            for channel_id in contract.quote_channel_ids:
                quote_bars = await self._store.load_quote_candles(
                    instrument,
                    source_id=channel_id,
                    interval=selected_interval,
                    start=start,
                    count=count,
                )
                for candle in quote_bars:
                    if candle.instrument != instrument or candle.source.provider != channel_id:
                        raise RuntimeError("source-bound quote cache returned foreign evidence")
                    value = self._projection_from_quote(source_id, candle)
                    rows[candle.open_time] = self._merge_for_read(
                        rows.get(candle.open_time),
                        value,
                    )

        end = start + selected_interval * count if start is not None else None
        for (
            candidate_source,
            candidate_instrument,
            candidate_interval,
            open_time,
        ), value in self._bars.items():
            if (
                candidate_source != source_id
                or candidate_instrument != instrument
                or candidate_interval != selected_interval
            ):
                continue
            if start is not None and open_time < start:
                continue
            if end is not None and open_time >= end:
                continue
            rows[open_time] = self._merge_for_read(rows.get(open_time), value)
            if value.state is not BarState.PROVISIONAL_QUOTE:
                authoritative_times.append(open_time)

        if (
            authoritative_times
            and contract.finality_policy is BarFinalityPolicy.NEXT_AUTHORITATIVE_BAR
        ):
            watermark = max(authoritative_times)
            for open_time, value in tuple(rows.items()):
                if open_time < watermark and value.state is BarState.PROVISIONAL_AUTHORITATIVE:
                    rows[open_time] = replace(
                        value,
                        state=BarState.FINAL,
                        revision=value.revision + 1,
                        finalized_at=value.source.received_at,
                    )

        if selected_interval != contract.interval and rows:
            latest_open_time = max(rows)
            for open_time, value in tuple(rows.items()):
                if open_time < latest_open_time and value.state is BarState.PROVISIONAL_QUOTE:
                    rows[open_time] = replace(
                        value,
                        state=BarState.FINAL,
                        revision=value.revision + 1,
                        finalized_at=value.source.received_at,
                    )

        ordered = sorted(rows.values(), key=lambda item: item.open_time)
        return tuple(ordered[:count] if start is not None else ordered[-count:])

    async def get_candles(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[RealtimeBar, ...]:
        """Compatibility name for REST callers while the public resource remains candles."""

        return await self.get_bars(instrument, source_id=source_id, start=start, count=count)

    async def get_bars_before(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta | None = None,
        before: datetime | None = None,
        count: int = 2_000,
    ) -> tuple[RealtimeBar, ...]:
        """Reads one internal cursor page; callers may continue until the page is empty."""

        if not 1 <= count <= REALTIME_BAR_READ_PAGE_SIZE_MAX:
            raise ValueError("cursor page count must be between 1 and 10000")
        if before is not None and (before.tzinfo is None or before.utcoffset() is None):
            raise ValueError("before must be timezone-aware")
        contract = self._contract(source_id)
        selected_interval = self._projection_interval(contract, interval)
        rows: dict[datetime, RealtimeBar] = {}
        authoritative_times: list[datetime] = []
        if self._store is not None:
            projected = await self._store.load_realtime_bars_before(
                instrument,
                source_id=source_id,
                interval=selected_interval,
                before=before,
                count=count,
            )
            for value in projected:
                rows[value.open_time] = value
                if value.state is not BarState.PROVISIONAL_QUOTE:
                    authoritative_times.append(value.open_time)

            # `realtime_bars` is the canonical, source-neutral series. Legacy raw
            # candles and quote-derived bars are only a fallback for a cursor page
            # that predates canonical storage; merging every evidence channel on
            # every internal page turns large period reads into repeated full
            # scans of the same history.
            if not projected and selected_interval == contract.interval:
                raw_bars = await self._store.load_source_candles_before(
                    instrument,
                    source_id=contract.authoritative_bar_channel_id,
                    interval=selected_interval,
                    before=before,
                    count=count,
                )
                for candle in raw_bars:
                    value = self._projection_from_raw_bar(source_id, candle)
                    rows[candle.open_time] = self._merge_for_read(rows.get(candle.open_time), value)
                    authoritative_times.append(candle.open_time)

            if not rows:
                for channel_id in contract.quote_channel_ids:
                    quote_bars = await self._store.load_quote_candles_before(
                        instrument,
                        source_id=channel_id,
                        interval=selected_interval,
                        before=before,
                        count=count,
                    )
                    for candle in quote_bars:
                        value = self._projection_from_quote(source_id, candle)
                        rows[candle.open_time] = self._merge_for_read(
                            rows.get(candle.open_time),
                            value,
                        )

        for (
            candidate_source,
            candidate_instrument,
            candidate_interval,
            open_time,
        ), value in self._bars.items():
            if (
                candidate_source != source_id
                or candidate_instrument != instrument
                or candidate_interval != selected_interval
                or (before is not None and open_time >= before)
            ):
                continue
            rows[open_time] = self._merge_for_read(rows.get(open_time), value)
            if value.state is not BarState.PROVISIONAL_QUOTE:
                authoritative_times.append(open_time)

        if (
            authoritative_times
            and contract.finality_policy is BarFinalityPolicy.NEXT_AUTHORITATIVE_BAR
        ):
            watermark = max(authoritative_times)
            for open_time, value in tuple(rows.items()):
                if open_time < watermark and value.state is BarState.PROVISIONAL_AUTHORITATIVE:
                    rows[open_time] = replace(
                        value,
                        state=BarState.FINAL,
                        revision=value.revision + 1,
                        finalized_at=value.source.received_at,
                    )

        if selected_interval != contract.interval and rows:
            latest_open_time = max(rows)
            for open_time, value in tuple(rows.items()):
                if open_time < latest_open_time and value.state is BarState.PROVISIONAL_QUOTE:
                    rows[open_time] = replace(
                        value,
                        state=BarState.FINAL,
                        revision=value.revision + 1,
                        finalized_at=value.source.received_at,
                    )

        return tuple(sorted(rows.values(), key=lambda item: item.open_time)[-count:])

    async def hydrate(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        count: int = 240,
    ) -> tuple[RealtimeBar, ...]:
        """Restores the hot state machine from local storage before acquisition starts."""

        contract = self._contract(source_id)
        if self._store is not None:
            state = await self._store.load_realtime_bar_series_state(
                instrument,
                realtime_source_id=source_id,
                interval=contract.interval,
            )
            if state is not None:
                series_key = (source_id, instrument, contract.interval)
                self._series_states[series_key] = state
                self._authoritative_watermarks[series_key] = state.authoritative_through
        authoritative_rows: tuple[RealtimeBar, ...] = ()
        hydrated: list[RealtimeBar] = []
        for interval in contract.quote_projection_intervals:
            rows = await self.get_bars(
                instrument,
                source_id=source_id,
                interval=interval,
                count=count,
            )
            if interval == contract.interval:
                authoritative_rows = rows
            for value in rows:
                self._bars[(source_id, instrument, interval, value.open_time)] = value
                series_key = (source_id, instrument, interval)
                watermark = self._watermarks.get(series_key)
                if watermark is None or value.open_time > watermark:
                    self._watermarks[series_key] = value.open_time
            hydrated.extend(rows)
            self._trim(source_id, instrument, interval)
        if hydrated and self._store is not None:
            await self._store.save_realtime_bars(hydrated)
        return authoritative_rows

    async def backfill(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime,
        count: int,
        revalidate: bool = False,
    ) -> BarBackfillResult:
        """Coalesces one same-source fetch and runs history through the same reducer.

        ``revalidate`` is reserved for a concrete, observed in-session gap. It bypasses
        the coarse completed-window cache without changing source identity or using a
        fallback provider.
        """

        self._validate_window(start, count)
        self._contract(source_id)
        key = (source_id, instrument, start, count, revalidate)
        task = self._backfills.get(key)
        joined = task is not None
        if task is None:
            task = asyncio.create_task(
                self._backfill_once(
                    instrument,
                    source_id=source_id,
                    start=start,
                    count=count,
                    revalidate=revalidate,
                ),
                name=f"Bar-backfill:{source_id}:{instrument.symbol}",
            )
            self._backfills[key] = task
        try:
            result = await asyncio.shield(task)
            if joined:
                self._backfill_metric_counts["joined_calls"] += 1
                return replace(result, state=BarBackfillState.JOINED)
            return result
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
        revalidate: bool,
    ) -> BarBackfillResult:
        contract = self._contract(source_id)
        store = self._store
        provider = contract.history_provider
        if store is None:
            raise ProviderUnavailableError("本地历史存储不可用, 不能执行同源回补")
        if provider is None:
            raise ProviderUnavailableError(f"{source_id} 没有可用的同源历史 Bar 通道")
        end = start + contract.interval * count
        series_id = (source_id, instrument)
        series_key = (source_id, instrument, contract.interval)
        lock = self._backfill_locks.setdefault(series_id, asyncio.Lock())
        try:
            async with lock:
                now = self._now()
                state = await store.load_realtime_bar_series_state(
                    instrument,
                    realtime_source_id=source_id,
                    interval=contract.interval,
                )
                if state is not None:
                    self._series_states[series_key] = state

                failure = self._backfill_failures.get(series_id)
                if failure is not None and now < failure.retry_after:
                    return BarBackfillResult(
                        source_id,
                        BarBackfillState.DEFERRED,
                        start,
                        end,
                        0,
                        authoritative_through=(
                            state.authoritative_through if state is not None else None
                        ),
                        history_floor=(state.history_floor if state is not None else None),
                        retry_after=failure.retry_after,
                        evidence_version=(state.evidence_version if state is not None else None),
                    )

                evidence_version = state.evidence_version if state is not None else None
                revalidation_key = (source_id, instrument, start, end, evidence_version)
                if revalidate:
                    last_check = self._revalidation_checks.get(revalidation_key)
                    if last_check is not None and now < last_check + self._revalidate_cooldown:
                        return BarBackfillResult(
                            source_id,
                            BarBackfillState.DEFERRED,
                            start,
                            end,
                            0,
                            authoritative_through=(
                                state.authoritative_through if state is not None else None
                            ),
                            history_floor=(state.history_floor if state is not None else None),
                            retry_after=last_check + self._revalidate_cooldown,
                            evidence_version=(
                                state.evidence_version if state is not None else None
                            ),
                        )

                missing = (
                    ((start, end),)
                    if revalidate
                    else await store.candle_missing_ranges(
                        instrument,
                        realtime_source_id=source_id,
                        start=start,
                        end=end,
                        interval=contract.interval,
                    )
                )
                history_floor = state.history_floor if state is not None else None
                if history_floor is not None:
                    missing = tuple(
                        (max(range_start, history_floor), range_end)
                        for range_start, range_end in missing
                        if range_end > history_floor
                    )
                    if not missing and end <= history_floor:
                        self._backfill_metric_counts["cache_hits"] += 1
                        return BarBackfillResult(
                            source_id,
                            BarBackfillState.EXHAUSTED,
                            start,
                            end,
                            0,
                            authoritative_through=state.authoritative_through,
                            history_floor=history_floor,
                            evidence_version=state.evidence_version,
                        )
                if not missing:
                    self._backfill_metric_counts["cache_hits"] += 1
                    return BarBackfillResult(
                        source_id,
                        BarBackfillState.CACHED,
                        start,
                        end,
                        0,
                        covered_start=start,
                        covered_end=end,
                        authoritative_through=(
                            state.authoritative_through if state is not None else None
                        ),
                        history_floor=history_floor,
                        evidence_version=(state.evidence_version if state is not None else None),
                    )

                tail_retry_after: datetime | None = None
                if (
                    not revalidate
                    and state is not None
                    and state.tail_checked_through is not None
                    and state.tail_checked_at is not None
                ):
                    live_authority = self._authoritative_watermarks.get(series_key)
                    authority_advanced = (
                        live_authority is not None and live_authority > state.authoritative_through
                    )
                    retry_after = state.tail_checked_at + self._tail_cooldown
                    if not authority_advanced and now < retry_after:
                        missing = self._subtract_covered_window(
                            missing,
                            state.authoritative_through,
                            state.tail_checked_through,
                        )
                        tail_retry_after = retry_after
                if not missing:
                    self._backfill_metric_counts["cache_hits"] += 1
                    return BarBackfillResult(
                        source_id,
                        BarBackfillState.DEFERRED,
                        start,
                        end,
                        0,
                        authoritative_through=(
                            state.authoritative_through if state is not None else None
                        ),
                        history_floor=history_floor,
                        retry_after=tail_retry_after,
                        evidence_version=(state.evidence_version if state is not None else None),
                    )

                row_count = 0
                covered_start: datetime | None = None
                covered_end: datetime | None = None
                for missing_start, missing_end in missing:
                    missing_count = int((missing_end - missing_start) / contract.interval)
                    batch = await self._fetch_historical_batch(
                        provider,
                        instrument,
                        start=missing_start,
                        count=missing_count,
                    )
                    values = batch.candles
                    if (
                        batch.checked_start != missing_start
                        or batch.checked_end != missing_end
                        or batch.interval != contract.interval
                    ):
                        raise RuntimeError("same-source Bar provider checked an unexpected range")
                    for candle in values:
                        if (
                            candle.instrument != instrument
                            or candle.source.provider != contract.authoritative_bar_channel_id
                            or candle.interval != contract.interval
                            or not missing_start <= candle.open_time < missing_end
                        ):
                            raise RuntimeError("same-source Bar provider returned foreign evidence")

                    projections = self._historical_projections(
                        source_id,
                        contract.authoritative_bar_channel_id,
                        values,
                    )
                    state = await store.commit_historical_bar_batch(
                        instrument,
                        realtime_source_id=source_id,
                        upstream_channel_id=contract.authoritative_bar_channel_id,
                        provider_symbol=provider.provider_symbol(instrument),
                        batch=batch,
                        bars=projections,
                    )
                    self._publish_historical_projections(projections)
                    self._series_states[series_key] = state
                    self._authoritative_watermarks[series_key] = max(
                        self._authoritative_watermarks.get(
                            series_key,
                            state.authoritative_through,
                        ),
                        state.authoritative_through,
                    )
                    permanent_end = min(batch.checked_end, batch.authoritative_through)
                    if permanent_end > batch.checked_start:
                        covered_start = (
                            batch.checked_start
                            if covered_start is None
                            else min(covered_start, batch.checked_start)
                        )
                        covered_end = (
                            permanent_end
                            if covered_end is None
                            else max(covered_end, permanent_end)
                        )
                    row_count += len(values)
                    self._backfill_metric_counts["written_rows"] += len(values)

                self._backfill_failures.pop(series_id, None)
                if revalidate and state is not None:
                    self._revalidation_checks[
                        (source_id, instrument, start, end, state.evidence_version)
                    ] = now
                result_state = BarBackfillState.FETCHED if row_count else BarBackfillState.ADVANCED
                if (
                    row_count == 0
                    and state is not None
                    and state.history_floor is not None
                    and end <= state.history_floor
                ):
                    result_state = BarBackfillState.EXHAUSTED
                return BarBackfillResult(
                    source_id,
                    result_state,
                    start,
                    end,
                    row_count,
                    covered_start=covered_start,
                    covered_end=covered_end,
                    authoritative_through=(
                        state.authoritative_through if state is not None else None
                    ),
                    history_floor=(state.history_floor if state is not None else None),
                    evidence_version=(state.evidence_version if state is not None else None),
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._record_backfill_failure(series_id, error)
            raise

    async def _fetch_historical_batch(
        self,
        provider: HistoricalBarProvider,
        instrument: Instrument,
        *,
        start: datetime,
        count: int,
    ) -> HistoricalBarBatch:
        async with self._history_semaphore:
            self._backfill_metric_counts["upstream_calls"] += 1
            try:
                return await provider.fetch_historical_candles(
                    instrument,
                    start=start,
                    count=count,
                )
            except ProviderAuthenticationError:
                refresh = getattr(provider, "refresh_session", None)
                if not callable(refresh):
                    raise
                await refresh()
                self._backfill_metric_counts["upstream_calls"] += 1
                return await provider.fetch_historical_candles(
                    instrument,
                    start=start,
                    count=count,
                )

    def _record_backfill_failure(
        self,
        series_id: tuple[str, Instrument],
        error: Exception,
    ) -> None:
        previous = self._backfill_failures.get(series_id)
        count = (previous.count + 1) if previous is not None else 1
        multiplier = 2 ** min(count - 1, 16)
        delay = min(self._backoff_base * multiplier, self._backoff_max)
        failed_at = self._now()
        self._backfill_failures[series_id] = _BackfillFailure(
            count=count,
            retry_after=failed_at + delay,
        )
        self._backfill_metric_counts["failures"] += 1
        self._last_backfill_failure_at = failed_at
        self._last_backfill_failure_type = type(error).__name__

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("history clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _subtract_covered_window(
        ranges: tuple[tuple[datetime, datetime], ...],
        covered_start: datetime,
        covered_end: datetime,
    ) -> tuple[tuple[datetime, datetime], ...]:
        result: list[tuple[datetime, datetime]] = []
        for range_start, range_end in ranges:
            if range_end <= covered_start or range_start >= covered_end:
                result.append((range_start, range_end))
                continue
            if range_start < covered_start:
                result.append((range_start, covered_start))
            if range_end > covered_end:
                result.append((covered_end, range_end))
        return tuple(result)

    def _historical_projections(
        self,
        source_id: str,
        channel_id: str,
        candles: Sequence[Candle],
    ) -> tuple[RealtimeBar, ...]:
        projections: list[RealtimeBar] = []
        for candle in sorted(candles, key=lambda item: item.open_time):
            key = (source_id, candle.instrument, candle.interval, candle.open_time)
            current = self._bars.get(key)
            projections.append(
                RealtimeBar(
                    instrument=candle.instrument,
                    interval=candle.interval,
                    open_time=candle.open_time,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    source=self._public_metadata(
                        source_id,
                        channel_id,
                        candle.source,
                        derivation="authoritative_history",
                    ),
                    evidence_channel_id=channel_id,
                    state=BarState.FINAL,
                    revision=(current.revision + 1 if current is not None else 1),
                    finalized_at=candle.source.received_at,
                )
            )
        return tuple(projections)

    def _publish_historical_projections(self, bars: Sequence[RealtimeBar]) -> None:
        for value in bars:
            key = (
                value.source.provider,
                value.instrument,
                value.interval,
                value.open_time,
            )
            self._bars[key] = value
            series_key = key[:3]
            watermark = self._watermarks.get(series_key)
            if watermark is None or value.open_time > watermark:
                self._watermarks[series_key] = value.open_time
            self._trim(*series_key)

    @staticmethod
    def _projection_from_raw_bar(source_id: str, candle: Candle) -> RealtimeBar:
        state = RealtimeBarService._state_hint(candle.source)
        return RealtimeBar(
            instrument=candle.instrument,
            interval=candle.interval,
            open_time=candle.open_time,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            source=RealtimeBarService._public_metadata(
                source_id,
                candle.source.provider,
                candle.source,
                derivation=(
                    "authoritative_history" if state is BarState.FINAL else "authoritative_bar"
                ),
            ),
            evidence_channel_id=candle.source.provider,
            state=state,
            finalized_at=(candle.source.received_at if state is BarState.FINAL else None),
        )

    @staticmethod
    def _projection_from_quote(source_id: str, candle: Candle) -> RealtimeBar:
        return RealtimeBar(
            instrument=candle.instrument,
            interval=candle.interval,
            open_time=candle.open_time,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=None,
            source=RealtimeBarService._public_metadata(
                source_id,
                candle.source.provider,
                candle.source,
                derivation="quote_event",
            ),
            evidence_channel_id=candle.source.provider,
            state=BarState.PROVISIONAL_QUOTE,
        )

    @staticmethod
    def _merge_for_read(current: RealtimeBar | None, incoming: RealtimeBar) -> RealtimeBar:
        if current is None:
            return incoming
        if current.state is BarState.FINAL:
            if (
                incoming.state is BarState.FINAL
                and incoming.source.received_at > current.source.received_at
            ):
                return replace(incoming, revision=max(current.revision + 1, incoming.revision))
            return current
        if incoming.state is BarState.FINAL:
            return replace(incoming, revision=max(current.revision + 1, incoming.revision))
        if current.state is BarState.PROVISIONAL_AUTHORITATIVE:
            if incoming.state is BarState.PROVISIONAL_QUOTE:
                if incoming.source.received_at < current.source.received_at:
                    return current
                return replace(
                    current,
                    high=max(current.high, incoming.high),
                    low=min(current.low, incoming.low),
                    close=incoming.close,
                    revision=max(current.revision, incoming.revision) + 1,
                )
            return (
                incoming if incoming.source.received_at >= current.source.received_at else current
            )
        if incoming.state is BarState.PROVISIONAL_AUTHORITATIVE:
            return incoming
        return incoming if incoming.source.received_at >= current.source.received_at else current

    @staticmethod
    def _same_projection(left: RealtimeBar, right: RealtimeBar) -> bool:
        return (
            left.open == right.open
            and left.high == right.high
            and left.low == right.low
            and left.close == right.close
            and left.volume == right.volume
            and left.state is right.state
            and left.evidence_channel_id == right.evidence_channel_id
        )

    @staticmethod
    def _coalesce(values: Sequence[RealtimeBar]) -> tuple[RealtimeBar, ...]:
        rows: dict[tuple[str, str, timedelta, datetime], RealtimeBar] = {}
        for value in values:
            key = (
                value.source.provider,
                value.instrument.symbol,
                value.interval,
                value.open_time,
            )
            rows[key] = value
        return tuple(rows.values())

    @staticmethod
    def _state_hint(metadata: SourceMetadata) -> BarState:
        raw = metadata.raw_payload
        if raw:
            value = raw.get("bar_state")
            if isinstance(value, str):
                try:
                    return BarState(value)
                except ValueError:
                    pass
            if raw.get("history_file"):
                return BarState.FINAL
        return BarState.PROVISIONAL_AUTHORITATIVE

    @staticmethod
    def _sequence(metadata: SourceMetadata) -> int | None:
        raw = metadata.raw_payload
        value = raw.get("sequence") if raw else None
        return value if isinstance(value, int) and value >= 0 else None

    @staticmethod
    def _public_metadata(
        source_id: str,
        evidence_channel_id: str,
        metadata: SourceMetadata,
        *,
        derivation: str,
        last_event_channel_id: str | None = None,
    ) -> SourceMetadata:
        return SourceMetadata(
            provider=source_id,
            provider_symbol=metadata.provider_symbol,
            observed_at=metadata.observed_at,
            received_at=metadata.received_at,
            raw_payload={
                "cache_scope": "realtime_source",
                "derivation": derivation,
                "evidence_channel_id": evidence_channel_id,
                "last_event_channel_id": last_event_channel_id or evidence_channel_id,
            },
        )

    @staticmethod
    def _floor_time(value: datetime, interval: timedelta) -> datetime:
        seconds = int(interval.total_seconds())
        epoch = int(value.timestamp())
        return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)

    @staticmethod
    def _validate_window(start: datetime | None, count: int) -> None:
        if not 1 <= count <= REALTIME_BAR_READ_PAGE_SIZE_MAX:
            raise ValueError("count must be between 1 and 10000")
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must be timezone-aware")

    def _contract(self, source_id: str) -> RealtimeBarContract:
        contract = self._contracts.get(source_id)
        if contract is None:
            raise ProviderUnavailableError(f"{source_id} has no source-bound Bar capability")
        return contract

    @staticmethod
    def _projection_interval(
        contract: RealtimeBarContract,
        interval: timedelta | None,
    ) -> timedelta:
        selected = interval or contract.interval
        if selected not in contract.quote_projection_intervals:
            raise ValueError("interval is not part of the realtime Bar contract")
        return selected

    def _trim(self, source_id: str, instrument: Instrument, interval: timedelta) -> None:
        keys = sorted(
            (
                key
                for key in self._bars
                if key[0] == source_id and key[1] == instrument and key[2] == interval
            ),
            key=lambda item: item[3],
        )
        for key in keys[:-240]:
            del self._bars[key]

    def live_count(self) -> int:
        return len(self._bars)

    def pending_backfill_count(self) -> int:
        return len(self._backfills)

    def backfill_metrics(self) -> BarBackfillMetrics:
        return BarBackfillMetrics(
            cache_hits=self._backfill_metric_counts["cache_hits"],
            upstream_calls=self._backfill_metric_counts["upstream_calls"],
            joined_calls=self._backfill_metric_counts["joined_calls"],
            written_rows=self._backfill_metric_counts["written_rows"],
            failures=self._backfill_metric_counts["failures"],
            pending=self.pending_backfill_count(),
            last_failure_at=self._last_backfill_failure_at,
            last_failure_type=self._last_backfill_failure_type,
        )

    def history_backfill_configured(self, source_id: str) -> bool:
        """Return source-level setup state without exposing internal channel topology."""

        return self._contract(source_id).history_provider is not None

    def series_state(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta | None = None,
    ) -> RealtimeBarSeriesState | None:
        contract = self._contract(source_id)
        selected_interval = interval or contract.interval
        return self._series_states.get((source_id, instrument, selected_interval))

    def known_series_states(self) -> tuple[RealtimeBarSeriesState, ...]:
        return tuple(
            sorted(
                self._series_states.values(),
                key=lambda value: (
                    value.realtime_source_id,
                    value.instrument_symbol,
                    value.interval,
                ),
            )
        )

    async def close(self) -> None:
        tasks = tuple(self._backfills.values())
        self._backfills.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
