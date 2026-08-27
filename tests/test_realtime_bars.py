from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tracefang.application.realtime_bars import (
    RealtimeBarContract,
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


class _Store:
    def __init__(self) -> None:
        self.missing_calls = 0
        self.saved_candles: list[Candle] = []
        self.saved_bars = []
        self.coverage_records: list[dict[str, object]] = []

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
        return self.values


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


if __name__ == "__main__":
    unittest.main()
