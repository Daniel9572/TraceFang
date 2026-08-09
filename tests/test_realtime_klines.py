from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from market_analysis.application.quotes import (
    QuoteQuality,
    QuoteView,
    RealtimeQuoteSnapshot,
)
from market_analysis.application.realtime_klines import (
    RealtimeKlineBinding,
    RealtimeKlineService,
)
from market_analysis.domain.errors import ProviderUnavailableError
from market_analysis.domain.models import (
    AssetClass,
    Candle,
    Instrument,
    QuoteSnapshot,
    SourceMetadata,
)

INSTRUMENT = Instrument("XAU/USD", AssetClass.SPOT, "XAU", "USD", "OTC")
START = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)


def metadata(source: str, at: datetime, *, history: bool = False) -> SourceMetadata:
    return SourceMetadata(
        provider=source,
        provider_symbol="XAUUSD.GOODS",
        observed_at=at,
        received_at=at,
        raw_payload={"history_file": "file"} if history else None,
    )


def quote(source: str, price: str, at: datetime) -> QuoteSnapshot:
    return QuoteSnapshot(
        instrument=INSTRUMENT,
        last=Decimal(price),
        open=None,
        high=None,
        low=None,
        volume=None,
        change=None,
        change_percent=None,
        source=metadata(source, at),
    )


def candle_at(
    source: str,
    at: datetime,
    price: str = "4200",
    *,
    history: bool = False,
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
        source=metadata(source, at, history=history),
    )


class FakeStore:
    def __init__(
        self,
        rows_by_source: dict[str, tuple[Candle, ...]] | None = None,
        quote_rows_by_source: dict[str, tuple[Candle, ...]] | None = None,
    ) -> None:
        self.rows_by_source = rows_by_source or {}
        self.quote_rows_by_source = quote_rows_by_source or {}
        self.source_calls: list[str] = []
        self.quote_calls: list[str] = []
        self.saved: list[Candle] = []
        self.coverage: set[tuple[str, datetime, datetime]] = set()
        self.coverage_records: list[dict[str, object]] = []

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

    async def load_quote_candles(self, _instrument, *, source_id, start=None, count=100):
        self.quote_calls.append(source_id)
        return self._window(self.quote_rows_by_source.get(source_id, ()), start, count)

    async def save_candles(self, candles):
        self.saved.extend(candles)
        for item in candles:
            rows = list(self.rows_by_source.get(item.source.provider, ()))
            rows = [row for row in rows if row.open_time != item.open_time]
            rows.append(item)
            self.rows_by_source[item.source.provider] = tuple(
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


class FakeHistoryProvider:
    name = "history-a"

    def __init__(
        self,
        rows: tuple[Candle, ...],
        *,
        release: asyncio.Event | None = None,
    ) -> None:
        self.rows = rows
        self.release = release
        self.started = asyncio.Event()
        self.calls = 0
        self.requests: list[tuple[datetime, int]] = []

    def provider_symbol(self, _instrument: Instrument) -> str:
        return "XAUUSD.GOODS"

    async def fetch_historical_candles(self, _instrument, *, start, count):
        self.calls += 1
        self.requests.append((start, count))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        end = start + timedelta(minutes=count)
        return tuple(row for row in self.rows if start <= row.open_time < end)


def binding(provider: FakeHistoryProvider | None = None) -> RealtimeKlineBinding:
    return RealtimeKlineBinding(
        realtime_source_id="source-a",
        history_channel_id="history-a",
        live_quote_channel_id="live-a",
        history_provider=provider,
    )


class RealtimeKlineServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_only_bound_history_and_live_channels(self) -> None:
        store = FakeStore(
            {"history-a": (candle_at("history-a", START, history=True),)},
            {"live-a": (candle_at("live-a", START + timedelta(minutes=1)),)},
        )
        service = RealtimeKlineService(store, bindings=(binding(),))

        rows = await service.get_candles(INSTRUMENT, source_id="source-a")

        self.assertEqual(store.source_calls, ["history-a"])
        self.assertEqual(store.quote_calls, ["live-a"])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.source.provider == "source-a" for row in rows))

    async def test_foreign_source_never_falls_back_to_registered_channel(self) -> None:
        service = RealtimeKlineService(FakeStore(), bindings=(binding(),))

        with self.assertRaisesRegex(ProviderUnavailableError, "no source-bound Kline"):
            await service.get_candles(INSTRUMENT, source_id="source-b")

    async def test_new_minute_opens_from_first_live_quote(self) -> None:
        service = RealtimeKlineService(None, bindings=(binding(),))
        service.accept_quote(quote("live-a", "4200", START + timedelta(seconds=59)))
        service.accept_quote(quote("live-a", "4210", START + timedelta(minutes=1, seconds=1)))

        rows = await service.get_candles(INSTRUMENT, source_id="source-a")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].open, Decimal("4210"))
        self.assertEqual(rows[1].high, Decimal("4210"))
        self.assertEqual(rows[1].low, Decimal("4210"))

    async def test_application_derived_view_builds_a_live_minute_bar(self) -> None:
        service = RealtimeKlineService(None, bindings=(binding(),))
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

    async def test_history_and_live_quotes_merge_within_same_realtime_source(self) -> None:
        store = FakeStore({"history-a": (candle_at("history-a", START, "4200", history=True),)})
        service = RealtimeKlineService(store, bindings=(binding(),))
        service.accept_quote(quote("live-a", "4215", START + timedelta(seconds=30)))

        rows = await service.get_candles(INSTRUMENT, source_id="source-a")

        self.assertEqual(rows[0].open, Decimal("4200"))
        self.assertEqual(rows[0].high, Decimal("4215"))
        self.assertEqual(rows[0].close, Decimal("4215"))

    async def test_local_read_never_calls_history_provider(self) -> None:
        provider = FakeHistoryProvider((candle_at("history-a", START, history=True),))
        service = RealtimeKlineService(FakeStore(), bindings=(binding(provider),))

        await service.get_candles(INSTRUMENT, source_id="source-a", start=START, count=60)

        self.assertEqual(provider.calls, 0)

    async def test_backfill_fetches_once_then_uses_persistent_coverage(self) -> None:
        row = candle_at("history-a", START, history=True)
        provider = FakeHistoryProvider((row,))
        store = FakeStore()
        service = RealtimeKlineService(store, bindings=(binding(provider),))

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

    async def test_overlapping_window_fetches_only_the_uncovered_tail(self) -> None:
        provider = FakeHistoryProvider(
            (candle_at("history-a", START + timedelta(minutes=55), history=True),)
        )
        store = FakeStore()
        store.coverage.add(("source-a", START, START + timedelta(minutes=50)))
        service = RealtimeKlineService(store, bindings=(binding(provider),))

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
        service = RealtimeKlineService(store, bindings=(binding(provider),))

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
        service = RealtimeKlineService(FakeStore(), bindings=(binding(provider),))

        first = asyncio.create_task(
            service.backfill(INSTRUMENT, source_id="source-a", start=START, count=60)
        )
        await provider.started.wait()
        second = asyncio.create_task(
            service.backfill(INSTRUMENT, source_id="source-a", start=START, count=60)
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
