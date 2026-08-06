from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from decimal import Decimal

from market_analysis.application.realtime import QuoteStreamCoordinator
from market_analysis.domain.errors import ProviderUnavailableError
from market_analysis.domain.models import QuoteSnapshot, SourceMetadata
from market_analysis.infrastructure.providers.jin10 import SPOT_GOLD


def quote(source: str, price: str = "4242.65") -> QuoteSnapshot:
    now = datetime.now(UTC)
    return QuoteSnapshot(
        instrument=SPOT_GOLD,
        last=Decimal(price),
        open=None,
        high=None,
        low=None,
        volume=None,
        change=None,
        change_percent=None,
        source=SourceMetadata(
            provider=source,
            provider_symbol="XAUUSD",
            observed_at=now,
            received_at=now,
        ),
    )


class QuoteStreamCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_is_published_without_trying_another_source(self) -> None:
        calls = []

        async def fetch(_instrument, source):
            calls.append(source)
            raise ProviderUnavailableError("offline")

        coordinator = QuoteStreamCoordinator(
            fetch_quote=fetch,
            record_quote=lambda _: None,
            poll_interval=lambda _: 60,
        )
        try:
            async with coordinator.subscribe(SPOT_GOLD, source="primary") as queue:
                self.assertEqual((await asyncio.wait_for(queue.get(), 1)).state, "connecting")
                event = await asyncio.wait_for(queue.get(), 1)
                self.assertEqual(event.state, "unavailable")
                self.assertEqual(calls, ["primary"])
        finally:
            await coordinator.close()

    async def test_success_is_recorded_and_broadcast(self) -> None:
        recorded = []

        async def fetch(_instrument, source):
            return quote(source)

        coordinator = QuoteStreamCoordinator(
            fetch_quote=fetch,
            record_quote=recorded.append,
            poll_interval=lambda _: 60,
        )
        try:
            async with coordinator.subscribe(SPOT_GOLD, source="primary") as queue:
                await asyncio.wait_for(queue.get(), 1)
                event = await asyncio.wait_for(queue.get(), 1)
                self.assertEqual(event.state, "live")
                self.assertEqual(event.quote.source.provider, "primary")
                self.assertEqual(recorded, [event.quote])
        finally:
            await coordinator.close()

    async def test_push_source_broadcasts_without_waiting_for_poll_interval(self) -> None:
        calls = []

        async def fetch(_instrument, source):
            calls.append(source)
            return quote(source)

        coordinator = QuoteStreamCoordinator(
            fetch_quote=fetch,
            record_quote=lambda _: None,
            poll_interval=lambda _: 60,
            is_push_source=lambda source: source == "local",
        )
        try:
            async with coordinator.subscribe(SPOT_GOLD, source="local") as queue:
                await asyncio.wait_for(queue.get(), 1)
                await asyncio.wait_for(queue.get(), 1)

                coordinator.publish_quote(quote("local", "4243.10"))
                event = await asyncio.wait_for(queue.get(), 0.05)

                self.assertEqual(event.quote.last, Decimal("4243.10"))
                self.assertEqual(calls, ["local"])
        finally:
            await coordinator.close()


if __name__ == "__main__":
    unittest.main()
