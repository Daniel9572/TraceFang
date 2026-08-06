from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from decimal import Decimal

from market_analysis.domain.models import QuoteSnapshot, SourceMetadata
from market_analysis.infrastructure.postgres.writer import BufferedMarketDataWriter
from market_analysis.infrastructure.providers.jin10 import SPOT_GOLD


class MemoryStore:
    def __init__(self) -> None:
        self.opened = False
        self.quotes = []

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.opened = False

    async def save_quote(self, quote) -> None:
        self.quotes.append(quote)

    async def save_candles(self, candles) -> None:
        return None


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


if __name__ == "__main__":
    unittest.main()
