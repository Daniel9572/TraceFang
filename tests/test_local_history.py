from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from market_analysis.application.history import LocalCandleHistoryService
from market_analysis.domain.models import AssetClass, Candle, Instrument, SourceMetadata

INSTRUMENT = Instrument("XAUUSD", AssetClass.METAL, "XAU", "USD", "OTC")


def candle(minute: int, *, source: str) -> Candle:
    open_time = datetime(2026, 8, 6, 8, minute, tzinfo=UTC)
    return Candle(
        instrument=INSTRUMENT,
        interval=timedelta(minutes=1),
        open_time=open_time,
        open=Decimal("4200"),
        high=Decimal("4201"),
        low=Decimal("4199"),
        close=Decimal("4200.5"),
        volume=None,
        source=SourceMetadata(
            provider=source,
            provider_symbol="XAUUSD",
            observed_at=open_time,
            received_at=open_time,
        ),
    )


class FakeStore:
    def __init__(self, rows: tuple[Candle, ...] = ()) -> None:
        self.rows = rows
        self.saved: list[tuple[Candle, ...]] = []
        self.loaded_sources: list[str] = []

    async def load_candles(self, _instrument, **kwargs) -> tuple[Candle, ...]:
        self.loaded_sources.append(kwargs["source_id"])
        return self.rows[: kwargs["count"]]

    async def save_candles(self, rows) -> None:
        values = tuple(rows)
        self.saved.append(values)
        self.rows = values


class LocalCandleHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_backfills_only_from_the_selected_source(self) -> None:
        store = FakeStore()
        calls: list[str] = []

        async def fetch(_instrument, source, _start, _count):
            calls.append(source)
            return (candle(0, source=source),)

        service = LocalCandleHistoryService(
            store,
            fetch_candles=fetch,
            backfill_delay_seconds=0.01,
        )
        rows = await service.get_candles(
            INSTRUMENT,
            source_id="metered",
            count=1,
        )

        self.assertEqual(rows, ())
        self.assertEqual(calls, [])
        await asyncio.sleep(0.05)
        self.assertEqual(calls, ["metered"])
        self.assertEqual(store.loaded_sources, ["metered"])
        self.assertEqual(store.saved, [(candle(0, source="metered"),)])
        await service.close()

    async def test_complete_local_history_does_not_call_provider(self) -> None:
        store = FakeStore((candle(0, source="jin10_local"),))
        calls: list[str] = []

        async def fetch(_instrument, source, _start, _count):
            calls.append(source)
            return ()

        service = LocalCandleHistoryService(
            store,
            fetch_candles=fetch,
            backfill_delay_seconds=0,
        )
        rows = await service.get_candles(
            INSTRUMENT,
            source_id="jin10_local",
            start=datetime(2026, 8, 6, 8, 0, tzinfo=UTC),
            count=1,
        )
        await asyncio.sleep(0)

        self.assertEqual(rows, store.rows)
        self.assertEqual(calls, [])
        await service.close()


if __name__ == "__main__":
    unittest.main()
