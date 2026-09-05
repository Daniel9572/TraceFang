from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tracefang.application.quotes import (
    QuoteQuality,
    QuoteView,
    RealtimeQuoteSnapshot,
)
from tracefang.application.realtime_bars import (
    HistoricalBarBatch,
    RealtimeBarContract,
    RealtimeBarSeriesState,
    RealtimeBarService,
)
from tracefang.domain.errors import ProviderAuthenticationError, ProviderUnavailableError
from tracefang.domain.market_events import (
    BarState,
    QuoteObservationKind,
    QuoteSample,
    RealtimeBar,
)
from tracefang.domain.models import (
    AssetClass,
    Candle,
    Instrument,
    QuoteSnapshot,
    SourceMetadata,
)

INSTRUMENT = Instrument("XAU/USD", AssetClass.SPOT, "XAU", "USD", "OTC")
START = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)


def metadata(
    source: str,
    at: datetime,
    *,
    history: bool = False,
    received_at: datetime | None = None,
    bar_state: BarState | None = None,
    observation_kind: str | None = None,
) -> SourceMetadata:
    raw_payload: dict[str, object] = {}
    if history:
        raw_payload["history_file"] = "file"
    if bar_state is not None:
        raw_payload["bar_state"] = bar_state.value
    if observation_kind is not None:
        raw_payload["observation_kind"] = observation_kind
    return SourceMetadata(
        provider=source,
        provider_symbol="XAUUSD.GOODS",
        observed_at=at,
        received_at=received_at or at,
        raw_payload=raw_payload or None,
    )


def quote(
    source: str,
    price: str,
    at: datetime,
    *,
    received_at: datetime | None = None,
    observation_kind: str | None = None,
) -> QuoteSnapshot:
    return QuoteSnapshot(
        instrument=INSTRUMENT,
        last=Decimal(price),
        open=None,
        high=None,
        low=None,
        volume=None,
        change=None,
        change_percent=None,
        source=metadata(
            source,
            at,
            received_at=received_at,
            observation_kind=observation_kind,
        ),
    )


def candle_at(
    source: str,
    at: datetime,
    price: str = "4200",
    *,
    history: bool = False,
    received_at: datetime | None = None,
    bar_state: BarState | None = None,
) -> Candle:
    value = Decimal(price)
    return Candle(
        instrument=INSTRUMENT,
        interval=timedelta(minutes=1),
        open_time=at,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=None,
        source=metadata(
            source,
            at,
            history=history,
            received_at=received_at,
            bar_state=bar_state,
        ),
    )


def realtime_bar_at(at: datetime, price: str = "4200") -> RealtimeBar:
    value = Decimal(price)
    return RealtimeBar(
        instrument=INSTRUMENT,
        interval=timedelta(minutes=1),
        open_time=at,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=None,
        source=metadata("source-a", at),
        evidence_channel_id="history-a",
        state=BarState.FINAL,
        revision=1,
        finalized_at=at + timedelta(minutes=1),
    )


class FakeStore:
    def __init__(
        self,
        rows_by_source: dict[str, tuple[Candle, ...]] | None = None,
        quote_rows_by_source: dict[str, tuple[Candle, ...]] | None = None,
        realtime_rows_by_source: dict[str, tuple[RealtimeBar, ...]] | None = None,
        quote_samples: tuple[QuoteSample, ...] = (),
    ) -> None:
        self.rows_by_source = rows_by_source or {}
        self.quote_rows_by_source = quote_rows_by_source or {}
        self.realtime_rows_by_source = realtime_rows_by_source or {}
        self.quote_samples = quote_samples
        self.source_calls: list[str] = []
        self.quote_calls: list[str] = []
        self.realtime_before_calls: list[str] = []
        self.source_before_calls: list[str] = []
        self.quote_before_calls: list[str] = []
        self.saved: list[Candle] = []
        self.saved_realtime: list[RealtimeBar] = []
        self.coverage: set[tuple[str, datetime, datetime]] = set()
        self.coverage_records: list[dict[str, object]] = []
        self.series_state: RealtimeBarSeriesState | None = None

    async def load_quote_event_page(
        self,
        _instrument,
        *,
        source_ids,
        before_id=None,
        page_size=2_000,
    ):
        rows = tuple(
            item
            for item in self.quote_samples
            if item.channel_id in source_ids
            and (before_id is None or (item.storage_id or 0) < before_id)
        )
        return rows[-page_size:]

    @staticmethod
    def _window(
        rows: tuple[Candle, ...],
        start: datetime | None,
        count: int,
    ) -> tuple[Candle, ...]:
        if start is None:
            return rows[-count:]
        end = start + timedelta(minutes=count)
        return tuple(row for row in rows if start <= row.open_time < end)[:count]

    async def load_source_candles(
        self,
        _instrument,
        *,
        source_id,
        interval=timedelta(minutes=1),
        start=None,
        count=100,
    ):
        self.source_calls.append(source_id)
        return self._window(self.rows_by_source.get(source_id, ()), start, count)

    async def load_realtime_bars(
        self,
        _instrument,
        *,
        source_id,
        interval=timedelta(minutes=1),
        start=None,
        count=100,
    ):
        del interval
        return self._window(self.realtime_rows_by_source.get(source_id, ()), start, count)

    async def load_quote_candles(
        self,
        _instrument,
        *,
        source_id,
        interval=timedelta(minutes=1),
        start=None,
        count=100,
    ):
        del interval
        self.quote_calls.append(source_id)
        return self._window(self.quote_rows_by_source.get(source_id, ()), start, count)

    @staticmethod
    def _before_window(rows, before, count):
        candidates = tuple(row for row in rows if before is None or row.open_time < before)
        return candidates[-count:]

    async def load_realtime_bars_before(
        self,
        _instrument,
        *,
        source_id,
        interval=timedelta(minutes=1),
        before=None,
        count=2_000,
    ):
        del interval
        self.realtime_before_calls.append(source_id)
        return self._before_window(self.realtime_rows_by_source.get(source_id, ()), before, count)

    async def load_source_candles_before(
        self,
        _instrument,
        *,
        source_id,
        interval=timedelta(minutes=1),
        before=None,
        count=2_000,
    ):
        del interval
        self.source_before_calls.append(source_id)
        return self._before_window(self.rows_by_source.get(source_id, ()), before, count)

    async def load_quote_candles_before(
        self,
        _instrument,
        *,
        source_id,
        interval=timedelta(minutes=1),
        before=None,
        count=2_000,
    ):
        del interval
        self.quote_before_calls.append(source_id)
        return self._before_window(self.quote_rows_by_source.get(source_id, ()), before, count)

    async def save_candles(self, candles):
        self.saved.extend(candles)
        for item in candles:
            rows = list(self.rows_by_source.get(item.source.provider, ()))
            rows = [row for row in rows if row.open_time != item.open_time]
            rows.append(item)
            self.rows_by_source[item.source.provider] = tuple(
                sorted(rows, key=lambda value: value.open_time)
            )

    async def save_realtime_bars(self, bars):
        self.saved_realtime.extend(bars)
        for item in bars:
            rows = list(self.realtime_rows_by_source.get(item.source.provider, ()))
            rows = [row for row in rows if row.open_time != item.open_time]
            rows.append(item)
            self.realtime_rows_by_source[item.source.provider] = tuple(
                sorted(rows, key=lambda value: value.open_time)
            )

    async def candle_missing_ranges(
        self,
        _instrument,
        *,
        realtime_source_id,
        start,
        end,
        interval=timedelta(minutes=1),
    ):
        ranges = sorted(
            (range_start, range_end)
            for source_id, range_start, range_end in self.coverage
            if source_id == realtime_source_id and range_end > start and range_start < end
        )
        missing = []
        covered_until = start
        for range_start, range_end in ranges:
            if range_end <= covered_until:
                continue
            if range_start > covered_until:
                missing.append((covered_until, range_start))
            covered_until = max(covered_until, range_end)
        if covered_until < end:
            missing.append((covered_until, end))
        return tuple(missing)

    async def record_candle_cache_range(
        self,
        _instrument,
        *,
        realtime_source_id,
        upstream_channel_id,
        provider_symbol,
        start,
        end,
        row_count,
        interval=timedelta(minutes=1),
    ):
        self.coverage.add((realtime_source_id, start, end))
        self.coverage_records.append(
            {
                "source": realtime_source_id,
                "channel": upstream_channel_id,
                "provider_symbol": provider_symbol,
                "start": start,
                "end": end,
                "row_count": row_count,
            }
        )

    async def load_realtime_bar_series_state(self, *_args, **_kwargs):
        return self.series_state

    async def commit_historical_bar_batch(
        self,
        instrument,
        *,
        realtime_source_id,
        upstream_channel_id,
        provider_symbol,
        batch,
        bars,
    ):
        await self.save_candles(batch.candles)
        await self.save_realtime_bars(bars)
        coverage_end = min(batch.checked_end, batch.authoritative_through)
        if coverage_end > batch.checked_start:
            await self.record_candle_cache_range(
                instrument,
                realtime_source_id=realtime_source_id,
                upstream_channel_id=upstream_channel_id,
                provider_symbol=provider_symbol,
                start=batch.checked_start,
                end=coverage_end,
                row_count=len(batch.candles),
                interval=batch.interval,
            )
        self.series_state = RealtimeBarSeriesState(
            realtime_source_id=realtime_source_id,
            instrument_symbol=instrument.symbol,
            upstream_channel_id=upstream_channel_id,
            provider_symbol=provider_symbol,
            interval=batch.interval,
            latest_authoritative_open_time=max(
                (row.open_time for row in batch.candles),
                default=None,
            ),
            authoritative_through=batch.authoritative_through,
            history_floor=batch.history_floor,
            tail_checked_through=(
                batch.checked_end if batch.checked_end > batch.authoritative_through else None
            ),
            tail_checked_at=(
                batch.checked_at if batch.checked_end > batch.authoritative_through else None
            ),
            evidence_version=batch.evidence_version,
            updated_at=batch.checked_at,
        )
        return self.series_state


class FakeHistoryProvider:
    name = "history-a"

    def __init__(
        self,
        rows: tuple[Candle, ...],
        *,
        release: asyncio.Event | None = None,
        authoritative_through: datetime | None = None,
        history_floor: datetime | None = None,
        failure: Exception | None = None,
        failures_remaining: int = 0,
    ) -> None:
        self.rows = rows
        self.release = release
        self.started = asyncio.Event()
        self.calls = 0
        self.requests: list[tuple[datetime, int]] = []
        self.authoritative_through = authoritative_through
        self.history_floor = history_floor
        self.failure = failure
        self.failures_remaining = failures_remaining
        self.refresh_calls = 0

    def provider_symbol(self, _instrument: Instrument) -> str:
        return "XAUUSD.GOODS"

    async def fetch_historical_candles(self, _instrument, *, start, count):
        self.calls += 1
        self.requests.append((start, count))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        end = start + timedelta(minutes=count)
        if self.failure is not None and self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise self.failure
        return HistoricalBarBatch(
            candles=tuple(row for row in self.rows if start <= row.open_time < end),
            checked_start=start,
            checked_end=end,
            authoritative_through=self.authoritative_through or end,
            evidence_version="fake-v1",
            checked_at=end,
            history_floor=self.history_floor,
        )

    async def refresh_session(self) -> None:
        self.refresh_calls += 1


class ConcurrencyHistoryProvider:
    name = "history-a"

    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0
        self.entered = 0
        self.two_started = asyncio.Event()
        self.release = asyncio.Event()

    def provider_symbol(self, _instrument: Instrument) -> str:
        return "EMPTY.PROVIDER"

    async def fetch_historical_candles(self, _instrument, *, start, count):
        self.active += 1
        self.entered += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active == 2:
            self.two_started.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        end = start + timedelta(minutes=count)
        return HistoricalBarBatch(
            candles=(),
            checked_start=start,
            checked_end=end,
            authoritative_through=end,
            evidence_version="concurrency-v1",
            checked_at=end,
        )


def binding(provider: FakeHistoryProvider | None = None) -> RealtimeBarContract:
    return RealtimeBarContract(
        source_id="source-a",
        authoritative_bar_channel_id="history-a",
        quote_channel_ids=("live-a",),
        history_provider=provider,
    )


class RealtimeBarServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_reports_history_setup_from_the_unified_source_contract(self) -> None:
        without_history = RealtimeBarService(None, contracts=(binding(),))
        with_history = RealtimeBarService(
            None,
            contracts=(binding(FakeHistoryProvider(())),),
        )

        self.assertFalse(without_history.history_backfill_configured("source-a"))
        self.assertTrue(with_history.history_backfill_configured("source-a"))

    async def test_hydrate_restores_persisted_series_authority(self) -> None:
        store = FakeStore()
        store.series_state = RealtimeBarSeriesState(
            realtime_source_id="source-a",
            instrument_symbol=INSTRUMENT.symbol,
            upstream_channel_id="history-a",
            provider_symbol="XAUUSD.GOODS",
            interval=timedelta(minutes=1),
            latest_authoritative_open_time=START,
            authoritative_through=START + timedelta(minutes=1),
            history_floor=None,
            tail_checked_through=None,
            tail_checked_at=None,
            evidence_version="persisted-v1",
            updated_at=START + timedelta(minutes=2),
        )
        service = RealtimeBarService(store, contracts=(binding(),))

        await service.hydrate(INSTRUMENT, source_id="source-a")

        self.assertEqual(service.series_state(INSTRUMENT, source_id="source-a"), store.series_state)

    async def test_cursor_page_uses_canonical_rows_without_rescanning_evidence_channels(
        self,
    ) -> None:
        canonical = (realtime_bar_at(START), realtime_bar_at(START + timedelta(minutes=1)))
        store = FakeStore(
            rows_by_source={"history-a": (candle_at("history-a", START),)},
            quote_rows_by_source={"live-a": (candle_at("live-a", START),)},
            realtime_rows_by_source={"source-a": canonical},
        )
        service = RealtimeBarService(store, contracts=(binding(),))

        rows = await service.get_bars_before(
            INSTRUMENT,
            source_id="source-a",
            before=START + timedelta(minutes=2),
        )

        self.assertEqual(rows, canonical)
        self.assertEqual(store.realtime_before_calls, ["source-a"])
        self.assertEqual(store.source_before_calls, [])
        self.assertEqual(store.quote_before_calls, [])

    async def test_cursor_page_falls_back_to_legacy_authoritative_rows_when_needed(self) -> None:
        historical = candle_at("history-a", START, history=True)
        store = FakeStore(
            rows_by_source={"history-a": (historical,)},
            quote_rows_by_source={"live-a": (candle_at("live-a", START),)},
        )
        service = RealtimeBarService(store, contracts=(binding(),))

        rows = await service.get_bars_before(
            INSTRUMENT,
            source_id="source-a",
            before=START + timedelta(minutes=1),
        )

        self.assertEqual([row.open_time for row in rows], [START])
        self.assertEqual(store.source_before_calls, ["history-a"])
        self.assertEqual(store.quote_before_calls, [])

    async def test_persisted_quote_samples_preserve_distinct_price_revisions(self) -> None:
        samples = tuple(
            QuoteSample(
                source_id="live-a",
                channel_id="live-a",
                event_id=f"stored-{index}",
                instrument=INSTRUMENT,
                provider_symbol="XAUUSD",
                observed_at=START,
                received_at=START + timedelta(microseconds=index),
                value=Decimal(4200 + index),
                storage_id=index,
            )
            for index in range(1, 4)
        )
        service = RealtimeBarService(FakeStore(quote_samples=samples), contracts=(binding(),))

        first = await service.get_quote_sample_page(
            INSTRUMENT,
            source_id="source-a",
            page_size=2,
        )
        second = await service.get_quote_sample_page(
            INSTRUMENT,
            source_id="source-a",
            before_id=first.next_cursor,
            page_size=2,
        )

        self.assertEqual([item.storage_id for item in first.items], [2, 3])
        self.assertTrue(first.has_more)
        self.assertEqual(first.next_cursor, 2)
        self.assertTrue(all(item.source_id == "source-a" for item in first.items))
        self.assertEqual([item.storage_id for item in second.items], [1])
        self.assertFalse(second.has_more)

    async def test_persisted_quote_samples_preserve_same_price_same_time_events(self) -> None:
        values = ("4200", "4200", "4201", "4200")
        samples = tuple(
            QuoteSample(
                source_id="live-a",
                channel_id="live-a",
                event_id=f"stored-{index}",
                instrument=INSTRUMENT,
                provider_symbol="XAUUSD",
                observed_at=START,
                received_at=START + timedelta(microseconds=index),
                value=Decimal(value),
                storage_id=index,
            )
            for index, value in enumerate(values, start=1)
        )
        service = RealtimeBarService(FakeStore(quote_samples=samples), contracts=(binding(),))

        page = await service.get_quote_sample_page(
            INSTRUMENT,
            source_id="source-a",
            page_size=10,
        )

        self.assertEqual([item.storage_id for item in page.items], [1, 2, 3, 4])
        self.assertEqual(
            [item.value for item in page.items],
            [
                Decimal("4200"),
                Decimal("4200"),
                Decimal("4201"),
                Decimal("4200"),
            ],
        )

    async def test_live_business_samples_preserve_received_and_late_events(self) -> None:
        service = RealtimeBarService(None, contracts=(binding(),))
        frames = (
            quote("live-a", "4200", START, received_at=START),
            quote("live-a", "4200", START, received_at=START + timedelta(seconds=1)),
            quote("live-a", "4201", START, received_at=START + timedelta(seconds=2)),
            quote(
                "live-a",
                "4199",
                START - timedelta(seconds=1),
                received_at=START + timedelta(seconds=3),
            ),
        )

        samples = [
            service.sample_from_quote_event(event)
            for frame in frames
            if (event := service.normalize_quote(frame)) is not None
        ]

        self.assertTrue(all(sample is not None for sample in samples))
        self.assertEqual(
            [sample.value for sample in samples if sample is not None],
            [Decimal("4200"), Decimal("4200"), Decimal("4201"), Decimal("4199")],
        )
        self.assertEqual(len({sample.event_id for sample in samples if sample is not None}), 4)

        first_event = service.normalize_quote(frames[0])
        self.assertIsNotNone(first_event)
        replayed = service.sample_from_quote_event(first_event)
        self.assertEqual(replayed.event_id, samples[0].event_id)  # type: ignore[union-attr]

    async def test_business_sample_identity_never_deduplicates_across_sources(self) -> None:
        second = RealtimeBarContract(
            source_id="source-b",
            authoritative_bar_channel_id="history-b",
            quote_channel_ids=("live-b",),
        )
        service = RealtimeBarService(None, contracts=(binding(), second))
        source_a = service.normalize_quote(quote("live-a", "4200", START))
        source_b = service.normalize_quote(quote("live-b", "4200", START))

        self.assertIsNotNone(source_a)
        self.assertIsNotNone(source_b)
        self.assertIsNotNone(service.sample_from_quote_event(source_a))
        self.assertIsNotNone(service.sample_from_quote_event(source_b))

    async def test_business_sample_marks_a_polled_quote_as_snapshot(self) -> None:
        service = RealtimeBarService(None, contracts=(binding(),))
        event = service.normalize_quote(quote("live-a", "4200", START, observation_kind="snapshot"))

        self.assertIsNotNone(event)
        sample = service.sample_from_quote_event(event)

        self.assertEqual(sample.observation_kind, QuoteObservationKind.SNAPSHOT)

    async def test_multiple_realtime_sources_share_one_reducer_without_cross_talk(self) -> None:
        second = RealtimeBarContract(
            source_id="source-b",
            authoritative_bar_channel_id="history-b",
            quote_channel_ids=("live-b",),
        )
        service = RealtimeBarService(None, contracts=(binding(), second))

        service.accept_quote(quote("live-a", "4200", START + timedelta(seconds=5)))
        service.accept_quote(quote("live-b", "5200", START + timedelta(seconds=6)))

        source_a = await service.get_bars(INSTRUMENT, source_id="source-a")
        source_b = await service.get_bars(INSTRUMENT, source_id="source-b")

        self.assertEqual([row.close for row in source_a], [Decimal("4200")])
        self.assertEqual([row.close for row in source_b], [Decimal("5200")])
        self.assertEqual(source_a[0].source.provider, "source-a")
        self.assertEqual(source_b[0].source.provider, "source-b")

    async def test_reads_only_bound_history_and_live_channels(self) -> None:
        store = FakeStore(
            {"history-a": (candle_at("history-a", START, history=True),)},
            {"live-a": (candle_at("live-a", START + timedelta(minutes=1)),)},
        )
        service = RealtimeBarService(store, contracts=(binding(),))

        rows = await service.get_candles(INSTRUMENT, source_id="source-a")

        self.assertEqual(store.source_calls, ["history-a"])
        self.assertEqual(store.quote_calls, ["live-a"])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.source.provider == "source-a" for row in rows))

    async def test_foreign_source_never_falls_back_to_registered_channel(self) -> None:
        service = RealtimeBarService(FakeStore(), contracts=(binding(),))

        with self.assertRaisesRegex(ProviderUnavailableError, "no source-bound Bar"):
            await service.get_candles(INSTRUMENT, source_id="source-b")

    async def test_new_minute_opens_from_first_live_quote(self) -> None:
        service = RealtimeBarService(None, contracts=(binding(),))
        service.accept_quote(quote("live-a", "4200", START + timedelta(seconds=59)))
        service.accept_quote(quote("live-a", "4210", START + timedelta(minutes=1, seconds=1)))

        rows = await service.get_candles(INSTRUMENT, source_id="source-a")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].open, Decimal("4210"))
        self.assertEqual(rows[1].high, Decimal("4210"))
        self.assertEqual(rows[1].low, Decimal("4210"))
        self.assertEqual(rows[1].state, BarState.PROVISIONAL_QUOTE)

    async def test_each_quote_returns_complete_one_second_and_minute_upserts(self) -> None:
        service = RealtimeBarService(None, contracts=(binding(),))
        first_event = service.normalize_quote(
            quote("live-a", "4200", START + timedelta(milliseconds=100))
        )
        assert first_event is not None

        first = service.apply(first_event)

        self.assertEqual(
            [item.interval for item in first],
            [
                timedelta(seconds=1),
                timedelta(minutes=1),
            ],
        )
        self.assertTrue(all(item.open == item.close == Decimal("4200") for item in first))

        second_event = service.normalize_quote(
            quote("live-a", "4210", START + timedelta(milliseconds=800))
        )
        assert second_event is not None
        second = service.apply(second_event)
        second_bar = next(item for item in second if item.interval == timedelta(seconds=1))

        self.assertEqual(second_bar.open, Decimal("4200"))
        self.assertEqual(second_bar.high, Decimal("4210"))
        self.assertEqual(second_bar.low, Decimal("4200"))
        self.assertEqual(second_bar.close, Decimal("4210"))
        self.assertEqual(second_bar.revision, 2)

    async def test_new_second_replaces_closed_bar_then_appends_new_bar(self) -> None:
        service = RealtimeBarService(None, contracts=(binding(),))
        first_event = service.normalize_quote(
            quote("live-a", "4200", START + timedelta(milliseconds=900))
        )
        assert first_event is not None
        service.apply(first_event)
        second_event = service.normalize_quote(
            quote("live-a", "4210", START + timedelta(seconds=1, milliseconds=100))
        )
        assert second_event is not None

        transitions = service.apply(second_event)

        self.assertEqual(len(transitions), 3)
        closed, opened, minute = transitions
        self.assertEqual(closed.interval, timedelta(seconds=1))
        self.assertEqual(closed.open_time, START)
        self.assertEqual(closed.state, BarState.FINAL)
        self.assertEqual(closed.revision, 2)
        self.assertIsNotNone(closed.finalized_at)
        self.assertEqual(opened.interval, timedelta(seconds=1))
        self.assertEqual(opened.open_time, START + timedelta(seconds=1))
        self.assertEqual(opened.state, BarState.PROVISIONAL_QUOTE)
        self.assertEqual(minute.interval, timedelta(minutes=1))

        second_rows = await service.get_bars(
            INSTRUMENT,
            source_id="source-a",
            interval=timedelta(seconds=1),
        )
        before_rows = await service.get_bars_before(
            INSTRUMENT,
            source_id="source-a",
            interval=timedelta(seconds=1),
            before=START + timedelta(seconds=2),
        )
        self.assertEqual(second_rows, before_rows)
        self.assertEqual(
            [row.state for row in second_rows],
            [
                BarState.FINAL,
                BarState.PROVISIONAL_QUOTE,
            ],
        )

    async def test_unsupported_projection_interval_is_rejected(self) -> None:
        service = RealtimeBarService(None, contracts=(binding(),))

        with self.assertRaisesRegex(ValueError, "not part of the realtime Bar contract"):
            await service.get_bars(
                INSTRUMENT,
                source_id="source-a",
                interval=timedelta(seconds=5),
            )

    async def test_out_of_order_quote_does_not_rewind_current_bar(self) -> None:
        service = RealtimeBarService(None, contracts=(binding(),))
        service.accept_quote(quote("live-a", "4210", START + timedelta(seconds=30)))

        accepted = service.accept_quote(quote("live-a", "4200", START + timedelta(seconds=20)))
        rows = await service.get_bars(INSTRUMENT, source_id="source-a")

        self.assertFalse(accepted)
        self.assertEqual(rows[0].close, Decimal("4210"))

    async def test_application_derived_view_builds_a_live_minute_bar(self) -> None:
        service = RealtimeBarService(None, contracts=(binding(),))
        view = QuoteView(
            source_id="source-a",
            quote=RealtimeQuoteSnapshot(
                instrument=INSTRUMENT,
                last=Decimal("986.42"),
                open=None,
                high=None,
                low=None,
                volume=None,
                change=None,
                change_percent=None,
                source=metadata("source-a", START + timedelta(seconds=20)),
            ),
            quality=QuoteQuality.DEGRADED,
            unavailable_fields=("open", "high", "low", "volume"),
            stale_fields=(),
            composed_at=START + timedelta(seconds=20),
        )

        self.assertTrue(service.accept_view(view))
        rows = await service.get_candles(INSTRUMENT, source_id="source-a")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].close, Decimal("986.42"))
        self.assertEqual(rows[0].source.provider, "source-a")

    async def test_final_authoritative_bar_rejects_late_quote_mutation(self) -> None:
        store = FakeStore({"history-a": (candle_at("history-a", START, "4200", history=True),)})
        service = RealtimeBarService(store, contracts=(binding(),))
        service.accept_quote(quote("live-a", "4215", START + timedelta(seconds=30)))

        rows = await service.get_candles(INSTRUMENT, source_id="source-a")

        self.assertEqual(rows[0].open, Decimal("4200"))
        self.assertEqual(rows[0].high, Decimal("4200"))
        self.assertEqual(rows[0].close, Decimal("4200"))
        self.assertEqual(rows[0].state, BarState.FINAL)

    async def test_authoritative_update_calibrates_quote_then_quote_overlays_current_bar(
        self,
    ) -> None:
        service = RealtimeBarService(None, contracts=(binding(),))
        service.accept_quote(quote("live-a", "4215", START + timedelta(seconds=10)))
        native = candle_at(
            "history-a",
            START,
            "4200",
            received_at=START + timedelta(seconds=20),
            bar_state=BarState.PROVISIONAL_AUTHORITATIVE,
        )

        self.assertTrue(service.accept_bar(native))
        service.accept_quote(quote("live-a", "4210", START + timedelta(seconds=30)))
        rows = await service.get_bars(INSTRUMENT, source_id="source-a")

        self.assertEqual(rows[0].open, Decimal("4200"))
        self.assertEqual(rows[0].high, Decimal("4210"))
        self.assertEqual(rows[0].close, Decimal("4210"))
        self.assertEqual(rows[0].state, BarState.PROVISIONAL_AUTHORITATIVE)
        self.assertEqual(rows[0].evidence_channel_id, "history-a")

    async def test_next_authoritative_bar_finalizes_previous_bar(self) -> None:
        service = RealtimeBarService(None, contracts=(binding(),))
        first = candle_at(
            "history-a",
            START,
            "4200",
            received_at=START + timedelta(seconds=20),
            bar_state=BarState.PROVISIONAL_AUTHORITATIVE,
        )
        second = candle_at(
            "history-a",
            START + timedelta(minutes=1),
            "4210",
            received_at=START + timedelta(minutes=1, seconds=5),
            bar_state=BarState.PROVISIONAL_AUTHORITATIVE,
        )

        service.accept_bar(first)
        service.accept_bar(second)
        service.accept_quote(quote("live-a", "4300", START + timedelta(seconds=40)))
        rows = await service.get_bars(INSTRUMENT, source_id="source-a")

        self.assertEqual(rows[0].state, BarState.FINAL)
        self.assertEqual(rows[0].close, Decimal("4200"))
        self.assertEqual(rows[1].state, BarState.PROVISIONAL_AUTHORITATIVE)
        authority = service.series_state(INSTRUMENT, source_id="source-a")
        self.assertIsNotNone(authority)
        assert authority is not None
        self.assertEqual(authority.latest_authoritative_open_time, START)
        self.assertEqual(authority.authoritative_through, START + timedelta(minutes=1))

    async def test_newer_authoritative_revision_can_correct_a_final_bar(self) -> None:
        service = RealtimeBarService(None, contracts=(binding(),))
        original = candle_at(
            "history-a",
            START,
            "4200",
            history=True,
            received_at=START + timedelta(minutes=2),
        )
        corrected = candle_at(
            "history-a",
            START,
            "4205",
            received_at=START + timedelta(minutes=3),
            bar_state=BarState.PROVISIONAL_AUTHORITATIVE,
        )

        service.accept_bar(original)
        service.accept_bar(corrected)
        rows = await service.get_bars(INSTRUMENT, source_id="source-a")

        self.assertEqual(rows[0].state, BarState.FINAL)
        self.assertEqual(rows[0].close, Decimal("4205"))
        self.assertEqual(rows[0].revision, 2)

    async def test_local_read_never_calls_history_provider(self) -> None:
        provider = FakeHistoryProvider((candle_at("history-a", START, history=True),))
        service = RealtimeBarService(FakeStore(), contracts=(binding(provider),))

        await service.get_candles(INSTRUMENT, source_id="source-a", start=START, count=60)

        self.assertEqual(provider.calls, 0)

    async def test_backfill_fetches_once_then_uses_persistent_coverage(self) -> None:
        row = candle_at("history-a", START, history=True)
        provider = FakeHistoryProvider((row,))
        store = FakeStore()
        service = RealtimeBarService(store, contracts=(binding(provider),))

        first = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
        )
        second = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
        )

        self.assertEqual(first.state, "fetched")
        self.assertEqual(second.state, "cached")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(store.saved, [row])
        self.assertEqual(store.coverage_records[0]["row_count"], 1)
        self.assertEqual(service.backfill_metrics().upstream_calls, 1)
        self.assertEqual(service.backfill_metrics().cache_hits, 1)
        self.assertEqual(service.backfill_metrics().written_rows, 1)

    async def test_overlapping_window_fetches_only_the_uncovered_tail(self) -> None:
        provider = FakeHistoryProvider(
            (candle_at("history-a", START + timedelta(minutes=55), history=True),)
        )
        store = FakeStore()
        store.coverage.add(("source-a", START, START + timedelta(minutes=50)))
        service = RealtimeBarService(store, contracts=(binding(provider),))

        result = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
        )

        self.assertEqual(result.state, "fetched")
        self.assertEqual(provider.requests, [(START + timedelta(minutes=50), 10)])
        self.assertEqual(store.coverage_records[0]["start"], START + timedelta(minutes=50))
        self.assertEqual(store.coverage_records[0]["end"], START + timedelta(minutes=60))

    async def test_empty_successful_range_is_cached_without_inventing_candles(self) -> None:
        provider = FakeHistoryProvider(())
        store = FakeStore()
        service = RealtimeBarService(store, contracts=(binding(provider),))

        first = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
        )
        second = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
        )

        self.assertEqual(first.row_count, 0)
        self.assertEqual(second.state, "cached")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(store.saved, [])

    async def test_concurrent_identical_backfills_share_one_upstream_request(self) -> None:
        release = asyncio.Event()
        provider = FakeHistoryProvider(
            (candle_at("history-a", START, history=True),),
            release=release,
        )
        service = RealtimeBarService(FakeStore(), contracts=(binding(provider),))

        first = asyncio.create_task(
            service.backfill(INSTRUMENT, source_id="source-a", start=START, count=60)
        )
        await provider.started.wait()
        second = asyncio.create_task(
            service.backfill(INSTRUMENT, source_id="source-a", start=START, count=60)
        )
        await asyncio.sleep(0)
        release.set()
        first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(provider.calls, 1)
        self.assertEqual(first_result.state, "fetched")
        self.assertEqual(second_result.state, "joined")

    async def test_concurrent_overlaps_wait_then_fetch_only_the_delta(self) -> None:
        release = asyncio.Event()
        provider = FakeHistoryProvider((), release=release)
        service = RealtimeBarService(FakeStore(), contracts=(binding(provider),))
        first = asyncio.create_task(
            service.backfill(INSTRUMENT, source_id="source-a", start=START, count=60)
        )
        await provider.started.wait()
        second = asyncio.create_task(
            service.backfill(
                INSTRUMENT,
                source_id="source-a",
                start=START + timedelta(minutes=30),
                count=60,
            )
        )
        await asyncio.sleep(0)

        release.set()
        await asyncio.gather(first, second)

        self.assertEqual(
            provider.requests,
            [
                (START, 60),
                (START + timedelta(minutes=60), 30),
            ],
        )

    async def test_mutable_tail_is_soft_cached_until_authority_advances(self) -> None:
        provider = FakeHistoryProvider(
            (),
            authoritative_through=START + timedelta(minutes=30),
        )

        def clock() -> datetime:
            return START + timedelta(minutes=61)

        service = RealtimeBarService(
            FakeStore(),
            contracts=(binding(provider),),
            clock=clock,
            tail_cooldown=timedelta(minutes=5),
        )

        first = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
        )
        second = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
        )

        self.assertEqual(first.state, "advanced")
        self.assertEqual(second.state, "deferred")
        self.assertIsNotNone(second.retry_after)
        self.assertEqual(provider.calls, 1)

        service.accept_bar(
            candle_at(
                "history-a",
                START + timedelta(minutes=31),
                bar_state=BarState.PROVISIONAL_AUTHORITATIVE,
            )
        )
        await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START + timedelta(minutes=30),
            count=30,
        )

        self.assertEqual(provider.calls, 2)

    async def test_failure_enters_backoff_without_advancing_coverage(self) -> None:
        provider = FakeHistoryProvider(
            (),
            failure=ProviderUnavailableError("offline"),
            failures_remaining=1,
        )
        store = FakeStore()
        service = RealtimeBarService(
            store,
            contracts=(binding(provider),),
            clock=lambda: START,
        )

        with self.assertRaisesRegex(ProviderUnavailableError, "offline"):
            await service.backfill(
                INSTRUMENT,
                source_id="source-a",
                start=START,
                count=60,
            )
        deferred = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
        )

        self.assertEqual(deferred.state, "deferred")
        self.assertEqual(store.coverage, set())
        self.assertEqual(provider.calls, 1)

    async def test_authentication_failure_refreshes_and_retries_once(self) -> None:
        provider = FakeHistoryProvider(
            (),
            failure=ProviderAuthenticationError("expired"),
            failures_remaining=1,
        )
        service = RealtimeBarService(FakeStore(), contracts=(binding(provider),))

        result = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
        )

        self.assertEqual(result.state, "advanced")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(provider.refresh_calls, 1)

    async def test_revalidate_is_deduplicated_by_evidence_version_and_cooldown(self) -> None:
        provider = FakeHistoryProvider(())
        service = RealtimeBarService(
            FakeStore(),
            contracts=(binding(provider),),
            clock=lambda: START + timedelta(hours=2),
        )

        first = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
            revalidate=True,
        )
        second = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
            revalidate=True,
        )

        self.assertEqual(first.state, "advanced")
        self.assertEqual(second.state, "deferred")
        self.assertEqual(provider.calls, 1)

    async def test_explicit_history_floor_marks_exhaustion_without_guessing(self) -> None:
        provider = FakeHistoryProvider(
            (),
            history_floor=START + timedelta(minutes=60),
        )
        service = RealtimeBarService(FakeStore(), contracts=(binding(provider),))

        first = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
        )
        second = await service.backfill(
            INSTRUMENT,
            source_id="source-a",
            start=START,
            count=60,
        )

        self.assertEqual(first.state, "exhausted")
        self.assertEqual(second.state, "exhausted")
        self.assertEqual(first.history_floor, START + timedelta(minutes=60))
        self.assertEqual(provider.calls, 1)

    async def test_global_history_concurrency_is_bounded(self) -> None:
        provider = ConcurrencyHistoryProvider()
        service = RealtimeBarService(
            FakeStore(),
            contracts=(binding(provider),),  # type: ignore[arg-type]
            history_concurrency=2,
        )
        instruments = (
            INSTRUMENT,
            Instrument("XAG/USD", AssetClass.SPOT, "XAG", "USD", "OTC"),
            Instrument("XPT/USD", AssetClass.SPOT, "XPT", "USD", "OTC"),
        )
        tasks = [
            asyncio.create_task(
                service.backfill(
                    instrument,
                    source_id="source-a",
                    start=START,
                    count=60,
                )
            )
            for instrument in instruments
        ]

        await provider.two_started.wait()
        await asyncio.sleep(0)
        self.assertEqual(provider.entered, 2)
        self.assertEqual(provider.maximum_active, 2)
        provider.release.set()
        await asyncio.gather(*tasks)

        self.assertEqual(provider.entered, 3)
        self.assertEqual(provider.maximum_active, 2)

    async def test_waiting_history_does_not_block_realtime_bar_updates(self) -> None:
        release = asyncio.Event()
        provider = FakeHistoryProvider((), release=release)
        service = RealtimeBarService(FakeStore(), contracts=(binding(provider),))
        task = asyncio.create_task(
            service.backfill(INSTRUMENT, source_id="source-a", start=START, count=60)
        )
        await provider.started.wait()

        accepted = service.accept_quote(quote("live-a", "4210", START + timedelta(seconds=5)))

        self.assertTrue(accepted)
        self.assertGreater(service.live_count(), 0)
        release.set()
        await task

    async def test_cancelled_history_does_not_advance_state_or_stop_realtime(self) -> None:
        release = asyncio.Event()
        provider = FakeHistoryProvider((), release=release)
        store = FakeStore()
        service = RealtimeBarService(store, contracts=(binding(provider),))
        task = asyncio.create_task(
            service.backfill(INSTRUMENT, source_id="source-a", start=START, count=60)
        )
        await provider.started.wait()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(store.coverage, set())
        self.assertIsNone(store.series_state)
        self.assertEqual(service.backfill_metrics().failures, 0)
        self.assertTrue(service.accept_quote(quote("live-a", "4210", START + timedelta(seconds=5))))


if __name__ == "__main__":
    unittest.main()
