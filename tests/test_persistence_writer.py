from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tracefang.domain.market_events import BarState, RealtimeBar
from tracefang.domain.models import QuoteSnapshot, SourceMetadata
from tracefang.infrastructure.postgres.writer import BufferedMarketDataWriter
from tracefang.infrastructure.providers.jin10 import SPOT_GOLD


class MemoryStore:
    def __init__(self) -> None:
        self.opened = False
        self.quotes = []
        self.bars = []

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.opened = False

    async def save_quote(self, quote) -> None:
        self.quotes.append(quote)

    async def save_candles(self, candles) -> None:
        return None

    async def save_realtime_bars(self, bars) -> None:
        self.bars.extend(bars)


def quote() -> QuoteSnapshot:
    now = datetime.now(UTC)
    return QuoteSnapshot(
        instrument=SPOT_GOLD,
        last=Decimal("4242.65"),
        open=None,
        high=None,
        low=None,
        volume=None,
        change=None,
        change_percent=None,
        source=SourceMetadata(
            provider="structured",
            provider_symbol="XAUUSD",
            observed_at=now,
            received_at=now,
            raw_payload={"close": "4242.65"},
        ),
    )


def realtime_bar() -> RealtimeBar:
    now = datetime.now(UTC)
    return RealtimeBar(
        instrument=SPOT_GOLD,
        interval=timedelta(minutes=1),
        open_time=now.replace(second=0, microsecond=0),
        open=Decimal("4242.65"),
        high=Decimal("4242.65"),
        low=Decimal("4242.65"),
        close=Decimal("4242.65"),
        volume=None,
        source=SourceMetadata(
            provider="realtime-source",
            provider_symbol="XAUUSD",
            observed_at=now,
            received_at=now,
            raw_payload=None,
        ),
        evidence_channel_id="structured",
        state=BarState.PROVISIONAL_QUOTE,
    )


class PersistenceWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_writes_queued_quote_without_blocking_submitter(self) -> None:
        store = MemoryStore()
        writer = BufferedMarketDataWriter(store, reconnect_seconds=0.01)
        await writer.start()
        try:
            self.assertTrue(writer.submit_quote(quote()))
            for _ in range(50):
                if store.quotes:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(store.quotes), 1)
            self.assertEqual(writer.health().state, "healthy")
        finally:
            await writer.stop()

    async def test_writes_realtime_bar_projection_through_the_same_queue(self) -> None:
        store = MemoryStore()
        writer = BufferedMarketDataWriter(store, reconnect_seconds=0.01)
        await writer.start()
        try:
            self.assertTrue(writer.submit_realtime_bars((realtime_bar(),)))
            for _ in range(50):
                if store.bars:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(store.bars), 1)
            self.assertEqual(store.bars[0].state, BarState.PROVISIONAL_QUOTE)
        finally:
            await writer.stop()


if __name__ == "__main__":
    unittest.main()
