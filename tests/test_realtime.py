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
from market_analysis.application.realtime import QuoteStreamCoordinator
from market_analysis.domain.errors import ProviderUnavailableError
from market_analysis.domain.market_events import BarState, QuoteSample, RealtimeBar
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
        quote=RealtimeQuoteSnapshot(
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


def bar(
    source: str,
    *,
    interval: timedelta = timedelta(minutes=1),
    price: str = "4242.65",
) -> RealtimeBar:
    now = datetime.now(UTC)
    open_time = now.replace(microsecond=0)
    if interval == timedelta(minutes=1):
        open_time = open_time.replace(second=0)
    value = Decimal(price)
    return RealtimeBar(
        instrument=SPOT_GOLD,
        interval=interval,
        open_time=open_time,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=None,
        source=SourceMetadata(
            provider=source,
            provider_symbol="XAUUSD",
            observed_at=now,
            received_at=now,
        ),
        evidence_channel_id=source,
        state=BarState.PROVISIONAL_QUOTE,
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

    async def test_raw_sample_is_delivered_separately_from_latest_quote_view(self) -> None:
        async def load(_instrument, source):
            return view(source)

        coordinator = QuoteStreamCoordinator(load_quote=load)
        value = quote("jin10_web", "4244.10")
        sample = QuoteSample(
            source_id="jin10_client",
            channel_id="jin10_web",
            event_id="sample-1",
            instrument=value.instrument,
            provider_symbol=value.source.provider_symbol,
            observed_at=value.source.observed_at,
            received_at=value.source.received_at,
            value=value.last,
        )
        try:
            async with coordinator.subscribe(SPOT_GOLD, source="jin10_client") as queue:
                await asyncio.wait_for(queue.get(), 1)
                await asyncio.wait_for(queue.get(), 1)
                coordinator.publish_sample(sample)

                event = await asyncio.wait_for(queue.get(), 0.05)

                self.assertEqual(event.kind, "sample")
                self.assertEqual(event.sample, sample)
                self.assertIsNone(event.quote)

                value = bar("jin10_client")
                coordinator.publish_bar_update(value)
                bar_event = await asyncio.wait_for(queue.get(), 0.05)
                self.assertEqual(bar_event.kind, "bar")
                self.assertEqual(bar_event.period_id, "1m")
                self.assertEqual(bar_event.bar, value)
                self.assertIsNone(bar_event.quote)
                self.assertIsNone(bar_event.sample)
        finally:
            await coordinator.close()

    async def test_period_pumps_share_quotes_but_isolate_bar_updates(self) -> None:
        async def load(_instrument, source):
            return view(source)

        coordinator = QuoteStreamCoordinator(load_quote=load)
        try:
            async with (
                coordinator.subscribe(
                    SPOT_GOLD,
                    source="jin10_client",
                    period="1s",
                ) as timeline_queue,
                coordinator.subscribe(
                    SPOT_GOLD,
                    source="jin10_client",
                    period="1m",
                ) as minute_queue,
            ):
                for queue in (timeline_queue, minute_queue):
                    await asyncio.wait_for(queue.get(), 1)
                    await asyncio.wait_for(queue.get(), 1)

                coordinator.publish(view("jin10_client", "4243.10"))
                timeline_quote = await asyncio.wait_for(timeline_queue.get(), 0.05)
                minute_quote = await asyncio.wait_for(minute_queue.get(), 0.05)
                self.assertEqual(timeline_quote.period_id, "1s")
                self.assertEqual(minute_quote.period_id, "1m")

                second_bar = bar("jin10_client", interval=timedelta(seconds=1))
                coordinator.publish_bar_update(second_bar)
                timeline_bar = await asyncio.wait_for(timeline_queue.get(), 0.05)
                self.assertEqual(timeline_bar.bar, second_bar)
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(minute_queue.get(), 0.01)

                minute_bar = bar("jin10_client")
                coordinator.publish_bar_update(minute_bar)
                minute_event = await asyncio.wait_for(minute_queue.get(), 0.05)
                self.assertEqual(minute_event.bar, minute_bar)
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(timeline_queue.get(), 0.01)
        finally:
            await coordinator.close()


if __name__ == "__main__":
    unittest.main()
