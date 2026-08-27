from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from market_analysis.application.provider_frames import ProviderFrame
from market_analysis.application.realtime_bars import RealtimeBarContract
from market_analysis.application.replay import MarketReplayProjector
from market_analysis.domain.models import AssetClass, Instrument, QuoteSnapshot, SourceMetadata
from market_analysis.infrastructure.providers.tonghuashun_futures import (
    TONGHUASHUN_LIVE_FRAME_CHANNEL,
    TonghuashunFuturesProvider,
    TonghuashunFuturesSettings,
)
from market_analysis.infrastructure.providers.tonghuashun_futures.protocol import (
    TONGHUASHUN_HTTP_FRAME_ENCODING,
    TONGHUASHUN_HTTP_FRAME_VERSION,
    TonghuashunHttpFrameKind,
    TonghuashunHttpResponseFrame,
    encode_http_response_frame,
)
from market_analysis.instruments import SHFE_GOLD_WEIGHTED

INSTRUMENT = Instrument("XAU/USD", AssetClass.SPOT, "XAU", "USD", "OTC")
START = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def frame(sequence: int, *, received_at: datetime | None = None) -> ProviderFrame:
    return ProviderFrame(
        version=1,
        channel="jin10_web",
        connection_id="connection-1",
        sequence=sequence,
        received_at=received_at or START + timedelta(milliseconds=sequence * 100),
        encoding="wire",
        body=str(sequence).encode(),
    )


class MarketReplayProjectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_warms_first_bucket_then_replays_every_revision_in_provider_order(self) -> None:
        prices = {1: "99", 2: "100", 3: "102", 4: "100"}

        async def decode(value: ProviderFrame):
            observed_at = START + (
                timedelta(milliseconds=100)
                if value.sequence == 1
                else timedelta(seconds=1, milliseconds=value.sequence * 100)
            )
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
            for sequence in (1, 2, 3, 4):
                events.extend(await projector.accept_frame(sequence, frame(sequence)))
        finally:
            await projector.close()

        quotes = [event.quote.last for event in events if event.kind == "quote" and event.quote]
        bars = [event.bar for event in events if event.kind == "bar" and event.bar]
        self.assertEqual(
            quotes,
            [Decimal("99"), Decimal("100"), Decimal("102"), Decimal("100")],
        )
        self.assertEqual([bar.close for bar in bars], quotes[1:])
        self.assertEqual([bar.revision for bar in bars], [1, 2, 3])
        self.assertEqual(
            [event.stream_sequence for event in events if event.kind == "frame"],
            [1, 2, 3, 4],
        )

    async def test_one_minute_replay_omits_truncated_first_bucket(self) -> None:
        observed = {
            1: START + timedelta(seconds=30),
            2: START + timedelta(minutes=1, seconds=10),
        }

        async def decode(value: ProviderFrame):
            at = observed[value.sequence]
            return (
                QuoteSnapshot(
                    instrument=INSTRUMENT,
                    last=Decimal(100 + value.sequence),
                    open=None,
                    high=None,
                    low=None,
                    volume=None,
                    change=None,
                    change_percent=None,
                    source=SourceMetadata(
                        provider="jin10_web",
                        provider_symbol="XAUUSD",
                        observed_at=at,
                        received_at=at,
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
            period_id="1m",
            schedule=None,
        )
        try:
            first = await projector.accept_frame(1, frame(1, received_at=observed[1]))
            second = await projector.accept_frame(2, frame(2, received_at=observed[2]))
        finally:
            await projector.close()

        self.assertEqual([event.kind for event in first], ["frame", "quote"])
        bars = [event.bar for event in second if event.kind == "bar" and event.bar]
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].open_time, START + timedelta(minutes=1))
        self.assertEqual(bars[0].close, Decimal("102"))

    async def test_tonghuashun_raw_http_frames_feed_the_same_bar_projector(self) -> None:
        provider = TonghuashunFuturesProvider(TonghuashunFuturesSettings())

        def captured(sequence: int, clock: str, price: str) -> ProviderFrame:
            payload = {
                "qh_au8888": {
                    "name": "沪金加权",
                    "pre": "928.80",
                    "date": "20260810",
                    "dates": ["20260810"],
                    "tradeTime": ["0900-1015"],
                    "data": f"{clock},{price},100,{price},10",
                }
            }
            response = TonghuashunHttpResponseFrame(
                version=TONGHUASHUN_HTTP_FRAME_VERSION,
                kind=TonghuashunHttpFrameKind.TIME,
                provider_code="qh_au8888",
                capability="quote",
                request_url="https://d.10jqka.com.cn/v6/time/qh_au8888/last.js",
                status_code=200,
                content_type="application/javascript",
                text_encoding="utf-8",
                content=(
                    f"quotebridge_v6_time_qh_au8888_last_js("
                    f"{json.dumps(payload, ensure_ascii=True)})"
                ).encode(),
            )
            return ProviderFrame(
                version=TONGHUASHUN_HTTP_FRAME_VERSION,
                channel=TONGHUASHUN_LIVE_FRAME_CHANNEL,
                connection_id="http-capture-1",
                sequence=sequence,
                received_at=datetime(2026, 8, 10, 1, sequence - 1, 1, tzinfo=UTC),
                encoding=TONGHUASHUN_HTTP_FRAME_ENCODING,
                body=encode_http_response_frame(response),
            )

        async def decode(value: ProviderFrame):
            values = []
            await provider.ingest_frame(value, on_quote=values.append)
            return tuple(values)

        projector = MarketReplayProjector(
            contracts=(
                RealtimeBarContract(
                    source_id="tonghuashun_futures",
                    authoritative_bar_channel_id="tonghuashun_futures",
                    quote_channel_ids=("tonghuashun_futures",),
                    history_provider=provider,
                ),
            ),
            decode_frame=decode,
            instrument=SHFE_GOLD_WEIGHTED,
            source_id="tonghuashun_futures",
            period_id="1s",
            schedule=None,
        )
        try:
            first = await projector.accept_frame(1, captured(1, "0900", "950.0"))
            second = await projector.accept_frame(2, captured(2, "0901", "951.0"))
        finally:
            await projector.close()
            await provider.close()

        self.assertEqual([event.kind for event in first], ["frame", "quote"])
        bars = [event.bar for event in second if event.kind == "bar" and event.bar]
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, Decimal("951.0"))
        self.assertEqual(bars[0].source.provider, "tonghuashun_futures")


if __name__ == "__main__":
    unittest.main()
