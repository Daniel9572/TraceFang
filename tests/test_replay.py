from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from market_analysis.application.provider_frames import ProviderFrame
from market_analysis.application.realtime_bars import RealtimeBarContract
from market_analysis.application.replay import MarketReplayProjector
from market_analysis.domain.models import AssetClass, Instrument, QuoteSnapshot, SourceMetadata

INSTRUMENT = Instrument("XAU/USD", AssetClass.SPOT, "XAU", "USD", "OTC")
START = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def frame(sequence: int) -> ProviderFrame:
    return ProviderFrame(
        version=1,
        channel="jin10_web",
        connection_id="connection-1",
        sequence=sequence,
        received_at=START + timedelta(milliseconds=sequence * 100),
        encoding="wire",
        body=str(sequence).encode(),
    )


class MarketReplayProjectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_replays_every_same_second_revision_in_provider_order(self) -> None:
        prices = {1: "100", 2: "102", 3: "100"}

        async def decode(value: ProviderFrame):
            observed_at = START + timedelta(milliseconds=value.sequence * 100)
            return (
                QuoteSnapshot(
                    instrument=INSTRUMENT,
                    last=Decimal(prices[value.sequence]),
                    open=None,
                    high=None,
                    low=None,
                    volume=None,
                    change=None,
                    change_percent=None,
                    source=SourceMetadata(
                        provider="jin10_web",
                        provider_symbol="XAUUSD",
                        observed_at=observed_at,
                        received_at=value.received_at,
                        raw_payload={"sequence": value.sequence},
                    ),
                ),
            )

        projector = MarketReplayProjector(
            contracts=(
                RealtimeBarContract(
                    source_id="jin10_client",
                    authoritative_bar_channel_id="jin10_local",
                    quote_channel_ids=("jin10_web",),
                ),
            ),
            decode_frame=decode,
            instrument=INSTRUMENT,
            source_id="jin10_client",
            period_id="timeline",
            schedule=None,
        )
        try:
            events = []
            for sequence in (1, 2, 3):
                events.extend(await projector.accept_frame(sequence, frame(sequence)))
        finally:
            await projector.close()

        quotes = [event.quote.last for event in events if event.kind == "quote" and event.quote]
        bars = [event.bar for event in events if event.kind == "bar" and event.bar]
        self.assertEqual(quotes, [Decimal("100"), Decimal("102"), Decimal("100")])
        self.assertEqual([bar.close for bar in bars], quotes)
        self.assertEqual([bar.revision for bar in bars], [1, 2, 3])
        self.assertEqual(
            [event.stream_sequence for event in events if event.kind == "frame"],
            [1, 2, 3],
        )


if __name__ == "__main__":
    unittest.main()
