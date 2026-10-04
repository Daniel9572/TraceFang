from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tracefang.application.realtime_bars import (
    HistoricalBarBatch,
    RealtimeBarContract,
    RealtimeBarSeriesState,
    RealtimeBarService,
)
from tracefang.domain.models import (
    AssetClass,
    Candle,
    Instrument,
    SourceMetadata,
)

INSTRUMENT = Instrument("XAU/USD", AssetClass.SPOT, "XAU", "USD", "OTC")
START = datetime(2026, 8, 10, 1, tzinfo=UTC)


def candle(at: datetime) -> Candle:
    return Candle(
        instrument=INSTRUMENT,
        interval=timedelta(minutes=1),
        open_time=at,
        open=Decimal("4300"),
        high=Decimal("4301"),
        low=Decimal("4299"),
        close=Decimal("4300.5"),
        volume=None,
        source=SourceMetadata(
            provider="authoritative",
            provider_symbol="XAUUSD.GOODS",
            observed_at=at,
            received_at=at + timedelta(seconds=1),
        ),
    )


class HistoricalBarBatchTests(unittest.TestCase):
    def test_validates_authority_range_and_alignment(self) -> None:
        batch = HistoricalBarBatch(
            candles=(candle(START),),
            checked_start=START,
            checked_end=START + timedelta(minutes=2),
            authoritative_through=START + timedelta(minutes=1),
            evidence_version="manifest-v1",
            checked_at=START + timedelta(minutes=3),
        )

        self.assertEqual(batch.candles, (candle(START),))
        self.assertIsNone(batch.history_floor)

        with self.assertRaisesRegex(ValueError, "aligned"):
            HistoricalBarBatch(
                candles=(),
                checked_start=START,
                checked_end=START + timedelta(minutes=2),
                authoritative_through=START + timedelta(seconds=30),
                evidence_version="manifest-v1",
                checked_at=START,
            )

        with self.assertRaisesRegex(ValueError, "checked range"):
            HistoricalBarBatch(
                candles=(),
                checked_start=START,
                checked_end=START + timedelta(minutes=2),
                authoritative_through=START + timedelta(minutes=3),
                evidence_version="manifest-v1",
                checked_at=START,
            )


class _Store:
    def __init__(self) -> None:
        self.missing_calls = 0
        self.saved_candles: list[Candle] = []
        self.saved_bars = []
        self.coverage_records: list[dict[str, object]] = []
        self.series_state: RealtimeBarSeriesState | None = None

    async def candle_missing_ranges(self, *args, **kwargs):
        del args, kwargs
        self.missing_calls += 1
        return ()

    async def save_candles(self, values):
        self.saved_candles.extend(values)

    async def save_realtime_bars(self, values):
        self.saved_bars.extend(values)

    async def record_candle_cache_range(self, *args, **kwargs):
        del args
        self.coverage_records.append(kwargs)

    async def load_realtime_bar_series_state(self, *args, **kwargs):
        del args, kwargs
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
        self.saved_candles.extend(batch.candles)
        self.saved_bars.extend(bars)
        coverage_end = min(batch.checked_end, batch.authoritative_through)
        if coverage_end > batch.checked_start:
            self.coverage_records.append(
                {
                    "realtime_source_id": realtime_source_id,
                    "upstream_channel_id": upstream_channel_id,
                    "provider_symbol": provider_symbol,
                    "start": batch.checked_start,
                    "end": coverage_end,
                    "row_count": len(batch.candles),
                    "interval": batch.interval,
                }
            )
        self.series_state = RealtimeBarSeriesState(
            realtime_source_id=realtime_source_id,
            instrument_symbol=instrument.symbol,
            upstream_channel_id=upstream_channel_id,
            provider_symbol=provider_symbol,
            interval=batch.interval,
            latest_authoritative_open_time=(
                max((row.open_time for row in batch.candles), default=None)
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


class _Provider:
    name = "authoritative"

    def __init__(self, values: tuple[Candle, ...]) -> None:
        self.values = values
        self.calls: list[tuple[datetime, int]] = []

    def provider_symbol(self, instrument: Instrument) -> str:
        del instrument
        return "XAUUSD.GOODS"

    async def fetch_historical_candles(self, instrument, *, start, count):
        self.assert_instrument = instrument
        self.calls.append((start, count))
        end = start + timedelta(minutes=count)
        return HistoricalBarBatch(
            candles=self.values,
            checked_start=start,
            checked_end=end,
            authoritative_through=end,
            evidence_version="test-v1",
            checked_at=end,
        )


class _FailingStore(_Store):
    async def commit_historical_bar_batch(self, *args, **kwargs):
        del args, kwargs
        raise RuntimeError("simulated database failure")


class RealtimeBarBackfillTests(unittest.IsolatedAsyncioTestCase):
    def service(self, store: _Store, provider: _Provider) -> RealtimeBarService:
        return RealtimeBarService(
            store,  # type: ignore[arg-type]
            contracts=(
                RealtimeBarContract(
                    source_id="realtime",
                    authoritative_bar_channel_id="authoritative",
                    quote_channel_ids=("quote",),
                    history_provider=provider,
                ),
            ),
        )

    async def test_normal_backfill_uses_completed_coverage(self) -> None:
        store = _Store()
        provider = _Provider((candle(START),))

        result = await self.service(store, provider).backfill(
            INSTRUMENT,
            source_id="realtime",
            start=START,
            count=5,
        )

        self.assertEqual(result.state, "cached")
        self.assertEqual(provider.calls, [])
        self.assertEqual(store.missing_calls, 1)

    async def test_revalidation_bypasses_coarse_coverage_for_one_exact_gap(self) -> None:
        store = _Store()
        provider = _Provider((candle(START), candle(START + timedelta(minutes=1))))

        result = await self.service(store, provider).backfill(
            INSTRUMENT,
            source_id="realtime",
            start=START,
            count=2,
            revalidate=True,
        )

        self.assertEqual(result.state, "fetched")
        self.assertEqual(result.row_count, 2)
        self.assertEqual(provider.calls, [(START, 2)])
        self.assertEqual(store.missing_calls, 0)
        self.assertEqual(store.saved_candles, list(provider.values))
        self.assertEqual(len(store.coverage_records), 1)
        self.assertEqual(store.coverage_records[0]["start"], START)
        self.assertEqual(store.coverage_records[0]["end"], START + timedelta(minutes=2))

    async def test_failed_atomic_commit_does_not_publish_history_to_memory(self) -> None:
        store = _FailingStore()
        provider = _Provider((candle(START),))
        service = self.service(store, provider)

        with self.assertRaisesRegex(RuntimeError, "database failure"):
            await service.backfill(
                INSTRUMENT,
                source_id="realtime",
                start=START,
                count=1,
                revalidate=True,
            )

        self.assertEqual(service.live_count(), 0)
        self.assertIsNone(store.series_state)


if __name__ == "__main__":
    unittest.main()
