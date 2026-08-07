from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from decimal import Decimal

from market_analysis.application.quotes import (
    LogicalQuoteSnapshot,
    QuoteQuality,
    QuoteView,
)
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


def view(source: str, price: str = "4242.65") -> QuoteView:
    value = quote(source, price)
    return QuoteView(
        source_id=source,
        quote=LogicalQuoteSnapshot(
            instrument=value.instrument,
            last=value.last,
            open=value.open,
            high=value.high,
            low=value.low,
            volume=value.volume,
            change=value.change,
            change_percent=value.change_percent,
            source=value.source,
        ),
        quality=QuoteQuality.COMPLETE,
        unavailable_fields=(),
        stale_fields=(),
        composed_at=datetime.now(UTC),
    )


class QuoteStreamCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_cache_failure_is_published_without_fallback(self) -> None:
        calls = []

        async def load(_instrument, source):
            calls.append(source)
            raise ProviderUnavailableError("offline")

        coordinator = QuoteStreamCoordinator(load_quote=load)
        try:
            async with coordinator.subscribe(SPOT_GOLD, source="primary") as queue:
                self.assertEqual((await asyncio.wait_for(queue.get(), 1)).state, "connecting")
                event = await asyncio.wait_for(queue.get(), 1)
                self.assertEqual(event.state, "unavailable")
                self.assertEqual(calls, ["primary"])
        finally:
            await coordinator.close()

    async def test_local_cache_seed_is_broadcast_without_recording_or_polling(self) -> None:
        calls = []

        async def load(_instrument, source):
            calls.append(source)
            return view(source)

        coordinator = QuoteStreamCoordinator(load_quote=load)
        try:
            async with coordinator.subscribe(SPOT_GOLD, source="primary") as queue:
                await asyncio.wait_for(queue.get(), 1)
                event = await asyncio.wait_for(queue.get(), 1)
                self.assertEqual(event.state, "live")
                self.assertEqual(event.quote.source_id, "primary")
                await asyncio.sleep(0.02)
                self.assertEqual(calls, ["primary"])
        finally:
            await coordinator.close()

    async def test_published_view_is_delivered_in_current_event_loop_turn(self) -> None:
        async def load(_instrument, source):
            return view(source)

        coordinator = QuoteStreamCoordinator(load_quote=load)
        try:
            async with coordinator.subscribe(SPOT_GOLD, source="jin10_client") as queue:
                await asyncio.wait_for(queue.get(), 1)
                await asyncio.wait_for(queue.get(), 1)

                coordinator.publish(view("jin10_client", "4243.10"))
                event = await asyncio.wait_for(queue.get(), 0.05)

                self.assertEqual(event.quote.quote.last, Decimal("4243.10"))
        finally:
            await coordinator.close()

    async def test_more_than_eight_intermediate_frames_are_not_discarded(self) -> None:
        async def load(_instrument, source):
            return view(source)

        coordinator = QuoteStreamCoordinator(load_quote=load)
        try:
            async with coordinator.subscribe(SPOT_GOLD, source="jin10_web") as queue:
                await asyncio.wait_for(queue.get(), 1)
                await asyncio.wait_for(queue.get(), 1)
                for index in range(20):
                    coordinator.publish(view("jin10_web", f"{4243 + index}.10"))

                values = [
                    (await asyncio.wait_for(queue.get(), 1)).quote.quote.last for _ in range(20)
                ]

                self.assertEqual(values[0], Decimal("4243.10"))
                self.assertEqual(values[-1], Decimal("4262.10"))
                self.assertEqual(len(values), 20)
        finally:
            await coordinator.close()


if __name__ == "__main__":
    unittest.main()
