from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from market_analysis.application.quotes import QuoteView
from market_analysis.domain.errors import ProviderUnavailableError
from market_analysis.domain.market_events import (
    BarEvent,
    BarFinalityPolicy,
    BarState,
    MarketEvent,
    QuoteEvent,
    QuoteSample,
    RealtimeBar,
)
from market_analysis.domain.models import (
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
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]: ...

    async def load_quote_candles_before(
        self,
        instrument: Instrument,
        *,
        source_id: str,
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


class HistoricalBarProvider(Protocol):
    name: str

    def provider_symbol(self, instrument: Instrument) -> str: ...

    async def fetch_historical_candles(
        self,
        instrument: Instrument,
        *,
        start: datetime,
        count: int,
    ) -> tuple[Candle, ...]: ...


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


@dataclass(frozen=True, slots=True)
class BarBackfillResult:
    source_id: str
    state: str
    start: datetime
    end: datetime
    row_count: int


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
    ) -> None:
        if not contracts:
            raise ValueError("at least one realtime Bar contract is required")
        self._store = store
        self._writer = writer
        self._contracts = {item.source_id: item for item in contracts}
        if len(self._contracts) != len(contracts):
            raise ValueError("realtime Bar source ids must be unique")

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
        self._backfills: dict[
            tuple[str, Instrument, datetime, int],
            asyncio.Task[BarBackfillResult],
        ] = {}
        self._backfill_locks: dict[tuple[str, Instrument], asyncio.Lock] = {}

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

    def accept(self, event: MarketEvent) -> bool:
        transitions = self._apply(event)
        if transitions and self._writer is not None:
            self._writer.submit_realtime_bars(self._coalesce(transitions))
        return bool(transitions)

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
            event_id=self._sample_event_id(
                event.source_id,
                event.channel_id,
                value.source.provider_symbol,
                value.source.observed_at,
                value.source.received_at,
                value.last,
            ),
            instrument=value.instrument,
            provider_symbol=value.source.provider_symbol,
            observed_at=value.source.observed_at,
            received_at=value.source.received_at,
            value=value.last,
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
        items = tuple(
            replace(
                row,
                source_id=source_id,
                event_id=self._sample_event_id(
                    source_id,
                    row.channel_id,
                    row.provider_symbol,
                    row.observed_at,
                    row.received_at,
                    row.value,
                ),
            )
            for row in visible
        )
        next_cursor = min((item.storage_id for item in items if item.storage_id), default=None)
        return QuoteSamplePage(items, next_cursor, has_more)

    def accept_bar(self, candle: Candle) -> bool:
        event = self.normalize_bar(candle)
        return self.accept(event) if event is not None else False

    def accept_view(self, view: QuoteView) -> bool:
        """Adapts an application-derived quote without adding source-specific Bar logic."""

        if view.source_id not in self._contracts:
            return False
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
        return self.accept(
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
        quote = event.quote
        open_time = self._floor_time(quote.source.observed_at, contract.interval)
        series_key = (event.source_id, quote.instrument, contract.interval)
        watermark = self._watermarks.get(series_key)
        if watermark is not None and open_time < watermark:
            return []

        key = (*series_key, open_time)
        current = self._bars.get(key)
        if current is not None and current.state is BarState.FINAL:
            return []
        if current is not None and quote.source.received_at < current.source.received_at:
            return []

        if current is None:
            value = RealtimeBar(
                instrument=quote.instrument,
                interval=contract.interval,
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
            if high == current.high and low == current.low and quote.last == current.close:
                return []
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
                        "authoritative_bar_with_quote_overlay"
                        if authoritative
                        else "quote_event"
                    ),
                    last_event_channel_id=event.channel_id,
                ),
                evidence_channel_id=(
                    current.evidence_channel_id if authoritative else event.channel_id
                ),
                revision=current.revision + 1,
            )
        self._bars[key] = value
        self._trim(event.source_id, quote.instrument, contract.interval)
        return [value]

    def _apply_bar(self, event: BarEvent) -> list[RealtimeBar]:
        contract = self._contract(event.source_id)
        if event.channel_id != contract.authoritative_bar_channel_id:
            raise ValueError("Bar event channel is not authoritative for this realtime source")
        candle = event.candle
        if candle.interval != contract.interval:
            raise ValueError("Bar event interval does not match the realtime Bar contract")

        series_key = (event.source_id, candle.instrument, candle.interval)
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
                    "authoritative_history"
                    if state is BarState.FINAL
                    else "authoritative_bar"
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
            values.append(value)
        return values

    async def get_bars(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[RealtimeBar, ...]:
        """Reads local projections and raw same-source evidence without upstream calls."""

        self._validate_window(start, count)
        contract = self._contract(source_id)
        rows: dict[datetime, RealtimeBar] = {}
        authoritative_times: list[datetime] = []
        if self._store is not None:
            projected = await self._store.load_realtime_bars(
                instrument,
                source_id=source_id,
                interval=contract.interval,
                start=start,
                count=count,
            )
            for value in projected:
                if value.source.provider != source_id or value.instrument != instrument:
                    raise RuntimeError("realtime Bar cache returned foreign projection")
                rows[value.open_time] = value
                if value.state is not BarState.PROVISIONAL_QUOTE:
                    authoritative_times.append(value.open_time)

            raw_bars = await self._store.load_source_candles(
                instrument,
                source_id=contract.authoritative_bar_channel_id,
                interval=contract.interval,
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

        end = start + contract.interval * count if start is not None else None
        for (candidate_source, candidate_instrument, candidate_interval, open_time), value in (
            self._bars.items()
        ):
            if (
                candidate_source != source_id
                or candidate_instrument != instrument
                or candidate_interval != contract.interval
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
                if (
                    open_time < watermark
                    and value.state is BarState.PROVISIONAL_AUTHORITATIVE
                ):
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
        before: datetime | None = None,
        count: int = 2_000,
    ) -> tuple[RealtimeBar, ...]:
        """Reads one internal cursor page; callers may continue until the page is empty."""

        if not 1 <= count <= REALTIME_BAR_READ_PAGE_SIZE_MAX:
            raise ValueError("cursor page count must be between 1 and 10000")
        if before is not None and (before.tzinfo is None or before.utcoffset() is None):
            raise ValueError("before must be timezone-aware")
        contract = self._contract(source_id)
        rows: dict[datetime, RealtimeBar] = {}
        authoritative_times: list[datetime] = []
        if self._store is not None:
            projected = await self._store.load_realtime_bars_before(
                instrument,
                source_id=source_id,
                interval=contract.interval,
                before=before,
                count=count,
            )
            for value in projected:
                rows[value.open_time] = value
                if value.state is not BarState.PROVISIONAL_QUOTE:
                    authoritative_times.append(value.open_time)

            raw_bars = await self._store.load_source_candles_before(
                instrument,
                source_id=contract.authoritative_bar_channel_id,
                interval=contract.interval,
                before=before,
                count=count,
            )
            for candle in raw_bars:
                value = self._projection_from_raw_bar(source_id, candle)
                rows[candle.open_time] = self._merge_for_read(rows.get(candle.open_time), value)
                authoritative_times.append(candle.open_time)

            for channel_id in contract.quote_channel_ids:
                quote_bars = await self._store.load_quote_candles_before(
                    instrument,
                    source_id=channel_id,
                    before=before,
                    count=count,
                )
                for candle in quote_bars:
                    value = self._projection_from_quote(source_id, candle)
                    rows[candle.open_time] = self._merge_for_read(
                        rows.get(candle.open_time),
                        value,
                    )

        for (candidate_source, candidate_instrument, candidate_interval, open_time), value in (
            self._bars.items()
        ):
            if (
                candidate_source != source_id
                or candidate_instrument != instrument
                or candidate_interval != contract.interval
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

        return tuple(sorted(rows.values(), key=lambda item: item.open_time)[-count:])

    async def hydrate(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        count: int = 240,
    ) -> tuple[RealtimeBar, ...]:
        """Restores the hot state machine from local storage before acquisition starts."""

        rows = await self.get_bars(instrument, source_id=source_id, count=count)
        contract = self._contract(source_id)
        for value in rows:
            self._bars[(source_id, instrument, contract.interval, value.open_time)] = value
            if value.state is not BarState.PROVISIONAL_QUOTE:
                series_key = (source_id, instrument, contract.interval)
                watermark = self._watermarks.get(series_key)
                if watermark is None or value.open_time > watermark:
                    self._watermarks[series_key] = value.open_time
        if rows and self._store is not None:
            await self._store.save_realtime_bars(rows)
        self._trim(source_id, instrument, contract.interval)
        return rows

    async def backfill(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        start: datetime,
        count: int,
    ) -> BarBackfillResult:
        """Coalesces one same-source fetch and runs history through the same reducer."""

        self._validate_window(start, count)
        self._contract(source_id)
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
                name=f"Bar-backfill:{source_id}:{instrument.symbol}",
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
    ) -> BarBackfillResult:
        contract = self._contract(source_id)
        store = self._store
        provider = contract.history_provider
        if store is None:
            raise ProviderUnavailableError("本地历史存储不可用, 不能执行同源回补")
        if provider is None:
            raise ProviderUnavailableError(f"{source_id} 没有可用的同源历史 Bar 通道")
        end = start + contract.interval * count
        lock = self._backfill_locks.setdefault((source_id, instrument), asyncio.Lock())
        async with lock:
            missing = await store.candle_missing_ranges(
                instrument,
                realtime_source_id=source_id,
                start=start,
                end=end,
                interval=contract.interval,
            )
            if not missing:
                return BarBackfillResult(source_id, "cached", start, end, 0)

            row_count = 0
            for missing_start, missing_end in missing:
                missing_count = int((missing_end - missing_start) / contract.interval)
                values = await provider.fetch_historical_candles(
                    instrument,
                    start=missing_start,
                    count=missing_count,
                )
                for candle in values:
                    if (
                        candle.instrument != instrument
                        or candle.source.provider != contract.authoritative_bar_channel_id
                        or candle.interval != contract.interval
                        or not missing_start <= candle.open_time < missing_end
                    ):
                        raise RuntimeError("same-source Bar provider returned foreign evidence")

                transitions: list[RealtimeBar] = []
                for candle in sorted(values, key=lambda item: item.open_time):
                    transitions.extend(
                        self._apply(
                            BarEvent(
                                source_id=source_id,
                                channel_id=contract.authoritative_bar_channel_id,
                                candle=candle,
                                state=BarState.FINAL,
                                sequence=self._sequence(candle.source),
                                finalized_at=candle.source.received_at,
                            )
                        )
                    )
                await store.save_candles(values)
                if transitions:
                    await store.save_realtime_bars(self._coalesce(transitions))
                await store.record_candle_cache_range(
                    instrument,
                    realtime_source_id=source_id,
                    upstream_channel_id=contract.authoritative_bar_channel_id,
                    provider_symbol=provider.provider_symbol(instrument),
                    start=missing_start,
                    end=missing_end,
                    row_count=len(values),
                    interval=contract.interval,
                )
                row_count += len(values)
            return BarBackfillResult(source_id, "fetched", start, end, row_count)

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
                    "authoritative_history"
                    if state is BarState.FINAL
                    else "authoritative_bar"
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
                incoming
                if incoming.source.received_at >= current.source.received_at
                else current
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
    def _sample_event_id(
        source_id: str,
        channel_id: str,
        provider_symbol: str,
        observed_at: datetime,
        received_at: datetime,
        value: object,
    ) -> str:
        return "|".join(
            (
                source_id,
                channel_id,
                provider_symbol,
                observed_at.isoformat(timespec="microseconds"),
                received_at.isoformat(timespec="microseconds"),
                str(value),
            )
        )

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

    async def close(self) -> None:
        tasks = tuple(self._backfills.values())
        self._backfills.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
