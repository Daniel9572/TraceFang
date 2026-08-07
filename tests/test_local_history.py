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
        self.loaded_priorities: list[tuple[str, ...]] = []

    async def load_candles(self, _instrument, **kwargs) -> tuple[Candle, ...]:
        self.loaded_priorities.append(tuple(kwargs["source_priority"]))
        return self.rows[: kwargs["count"]]

    async def save_candles(self, rows) -> None:
        values = tuple(rows)
        self.saved.append(values)
        merged = {row.open_time: row for row in self.rows}
        merged.update((row.open_time, row) for row in values)
        self.rows = tuple(sorted(merged.values(), key=lambda row: row.open_time))

    async def standardize_candles(self, _instrument, **_kwargs) -> None:
        return None


class LocalCandleHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_a_history_series_mixed_from_raw_channels(self) -> None:
        store = FakeStore(
            (
                candle(0, source="jin10_web"),
                candle(1, source="jin10_local"),
            )
        )
        service = LocalCandleHistoryService(
            store,
            fetch_candles=lambda *_args: asyncio.sleep(0, result=()),
            source_priority=lambda: ("jin10_web", "jin10_local"),
            quote_derived_sources=lambda: ("jin10_web", "jin10_local"),
            backfill_sources=lambda: (),
        )

        with self.assertRaisesRegex(RuntimeError, "multiple raw channels"):
            await service.get_candles(
                INSTRUMENT,
                start=datetime(2026, 8, 6, 8, 0, tzinfo=UTC),
                count=2,
            )

        await service.close()

    async def test_returns_global_local_history_before_background_work(self) -> None:
        existing = (candle(0, source="jin10_mcp"),)
        store = FakeStore(existing)
        calls: list[str] = []

        async def fetch(_instrument, source, _start, _count):
            calls.append(source)
            return ()

        service = LocalCandleHistoryService(
            store,
            fetch_candles=fetch,
            source_priority=lambda: ("jin10_mcp", "jin10_local"),
            quote_derived_sources=lambda: ("jin10_local",),
            backfill_sources=lambda: ("jin10_local", "jin10_mcp"),
            backfill_delay_seconds=0,
        )
        rows = await service.get_candles(
            INSTRUMENT,
            start=datetime(2026, 8, 6, 8, 0, tzinfo=UTC),
            count=1,
        )
        await asyncio.sleep(0)

        self.assertEqual(rows, existing)
        self.assertEqual(calls, [])
        self.assertEqual(store.loaded_priorities, [("jin10_mcp", "jin10_local")])
        await service.close()

    async def test_backfill_uses_free_source_before_limited_source(self) -> None:
        store = FakeStore()
        calls: list[str] = []

        async def fetch(_instrument, source, _start, _count):
            calls.append(source)
            minute = 0 if source == "free" else 1
            return (candle(minute, source=source),)

        service = LocalCandleHistoryService(
            store,
            fetch_candles=fetch,
            source_priority=lambda: ("official", "free"),
            quote_derived_sources=lambda: ("free",),
            backfill_sources=lambda: ("free", "official"),
            backfill_delay_seconds=0.01,
        )
        rows = await service.get_candles(
            INSTRUMENT,
            start=datetime(2026, 8, 6, 8, 0, tzinfo=UTC),
            count=2,
        )

        self.assertEqual(rows, ())
        self.assertEqual(calls, [])
        await asyncio.sleep(0.05)
        self.assertEqual(calls, ["free", "official"])
        self.assertEqual(
            store.saved,
            [
                (candle(0, source="free"),),
                (candle(1, source="official"),),
            ],
        )
        await service.close()

    async def test_complete_free_backfill_does_not_spend_limited_source(self) -> None:
        store = FakeStore()
        calls: list[str] = []

        async def fetch(_instrument, source, _start, _count):
            calls.append(source)
            if source == "free":
                return (
                    candle(0, source=source),
                    candle(1, source=source),
                )
            return (candle(2, source=source),)

        service = LocalCandleHistoryService(
            store,
            fetch_candles=fetch,
            source_priority=lambda: ("official", "free"),
            quote_derived_sources=lambda: ("free",),
            backfill_sources=lambda: ("free", "official"),
            backfill_delay_seconds=0,
        )
        rows = await service.get_candles(
            INSTRUMENT,
            start=datetime(2026, 8, 6, 8, 0, tzinfo=UTC),
            count=2,
        )
        await asyncio.sleep(0.05)

        self.assertEqual(rows, ())
        self.assertEqual(calls, ["free"])
        self.assertEqual(store.rows, (candle(0, source="free"), candle(1, source="free")))
        await service.close()


if __name__ == "__main__":
    unittest.main()
