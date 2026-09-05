from __future__ import annotations

import struct
import unittest
from datetime import UTC, datetime
from decimal import Decimal

from tracefang.application.provider_frames import ProviderFrame
from tracefang.domain.errors import ProviderDataError
from tracefang.infrastructure.providers.jin10_web.protocol import (
    QUOTE_PUSH_PROTOCOL,
    Jin10WebWireQuote,
)
from tracefang.infrastructure.providers.jin10_web.provider import Jin10WebProvider
from tracefang.infrastructure.providers.jin10_web.settings import Jin10WebSettings


def _quote_frame(*, last_micros: int = 4_252_340_000) -> bytes:
    code = b"XAUUSD.GOODS"
    return b"".join(
        (
            struct.pack("<HH", QUOTE_PUSH_PROTOCOL, len(code)),
            code,
            struct.pack(
                "<Iqq",
                int(datetime.now(UTC).timestamp()),
                last_micros,
                4_246_000_000,
            ),
        )
    )


class _RecordingSink:
    def __init__(self, events: list[str], *, error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.frames: list[ProviderFrame] = []

    async def capture(self, frame: ProviderFrame) -> int:
        self.events.append("capture")
        self.frames.append(frame)
        if self.error is not None:
            raise self.error
        return len(self.frames)


class Jin10WebProviderTests(unittest.TestCase):
    def test_dispatches_every_decoded_quote_with_channel_sequence(self) -> None:
        provider = Jin10WebProvider(Jin10WebSettings())
        received = []
        provider.add_quote_listener(received.append)
        timestamp = int(datetime.now(UTC).timestamp())

        provider._store_quote(
            Jin10WebWireQuote(
                provider_code="XAUUSD.GOODS",
                timestamp=timestamp,
                last_micros=4_252_340_000,
                previous_close_micros=4_246_000_000,
            ),
            protocol=QUOTE_PUSH_PROTOCOL,
        )
        provider._store_quote(
            Jin10WebWireQuote(
                provider_code="XAUUSD.GOODS",
                timestamp=timestamp,
                last_micros=4_252_360_000,
                previous_close_micros=4_246_000_000,
            ),
            protocol=QUOTE_PUSH_PROTOCOL,
        )

        self.assertEqual(
            [quote.last for quote in received],
            [
                Decimal("4252.340000"),
                Decimal("4252.360000"),
            ],
        )
        self.assertEqual(received[0].source.raw_payload["sequence"], 1)
        self.assertEqual(received[1].source.raw_payload["sequence"], 2)
        self.assertEqual(received[1].source.raw_payload["channel"], "jin10_public_websocket")
        self.assertEqual(received[1].source.raw_payload["observation_kind"], "event")
        self.assertEqual(received[1].change, Decimal("6.360000"))

        provider.remove_quote_listener(received.append)
        self.assertEqual(len(provider._quote_listeners), 0)


class Jin10WebFrameIngestTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_frame_waits_for_capture_before_dispatch(self) -> None:
        events: list[str] = []
        sink = _RecordingSink(events)
        provider = Jin10WebProvider(Jin10WebSettings(), frame_sink=sink)
        provider.add_quote_listener(lambda _: events.append("quote"))
        received_at = datetime.now(UTC)
        body = _quote_frame()

        quote = await provider._capture_live_frame(
            body,
            connection_id="connection-1",
            sequence=7,
            received_at=received_at,
        )

        self.assertEqual(events, ["capture", "quote"])
        self.assertEqual(sink.frames[0].body, body)
        self.assertEqual(sink.frames[0].encoding, "wire")
        self.assertEqual(sink.frames[0].connection_id, "connection-1")
        self.assertEqual(sink.frames[0].sequence, 7)
        self.assertEqual(quote.source.received_at, received_at)
        self.assertEqual(quote.source.raw_payload["sequence"], 7)

    async def test_capture_failure_is_fail_closed(self) -> None:
        events: list[str] = []
        sink = _RecordingSink(events, error=RuntimeError("PubAck failed"))
        provider = Jin10WebProvider(Jin10WebSettings(), frame_sink=sink)
        provider.add_quote_listener(lambda _: events.append("quote"))

        with self.assertRaisesRegex(RuntimeError, "PubAck failed"):
            await provider._capture_live_frame(
                _quote_frame(),
                connection_id="connection-1",
                sequence=1,
                received_at=datetime.now(UTC),
            )

        self.assertEqual(events, ["capture"])
        self.assertEqual(provider._latest, {})

    async def test_malformed_and_unknown_frames_are_captured_before_decode(self) -> None:
        events: list[str] = []
        sink = _RecordingSink(events)
        provider = Jin10WebProvider(Jin10WebSettings(), frame_sink=sink)
        malformed = b"\x01"
        unknown = struct.pack("<H", 32_000) + b"opaque"

        await provider._capture_live_frame(
            malformed,
            connection_id="connection-1",
            sequence=1,
            received_at=datetime.now(UTC),
        )
        await provider._capture_live_frame(
            unknown,
            connection_id="connection-1",
            sequence=2,
            received_at=datetime.now(UTC),
        )

        self.assertEqual([frame.body for frame in sink.frames], [malformed, unknown])
        self.assertEqual([frame.sequence for frame in sink.frames], [1, 2])

    async def test_replay_ingest_uses_only_explicit_callback(self) -> None:
        events: list[str] = []
        sink = _RecordingSink(events)
        provider = Jin10WebProvider(Jin10WebSettings(), frame_sink=sink)
        live_quotes = []
        replay_quotes = []
        provider.add_quote_listener(live_quotes.append)
        frame = ProviderFrame(
            version=1,
            channel=provider.name,
            connection_id="recording-1",
            sequence=9,
            received_at=datetime.now(UTC),
            encoding="wire",
            body=_quote_frame(),
        )

        replayed = await provider.ingest_frame(frame, on_quote=replay_quotes.append)

        self.assertIs(replayed, replay_quotes[0])
        self.assertEqual(len(replay_quotes), 1)
        self.assertEqual(live_quotes, [])
        self.assertEqual(sink.frames, [])
        self.assertEqual(provider._latest, {})

    async def test_public_ingest_reports_malformed_recording(self) -> None:
        provider = Jin10WebProvider(Jin10WebSettings())
        frame = ProviderFrame(
            version=1,
            channel=provider.name,
            connection_id="recording-1",
            sequence=1,
            received_at=datetime.now(UTC),
            encoding="wire",
            body=b"\x01",
        )

        with self.assertRaises(ProviderDataError):
            await provider.ingest_frame(frame)


class Jin10WebSettingsTests(unittest.TestCase):
    def test_accepts_change_driven_zero_frequency(self) -> None:
        settings = Jin10WebSettings.from_env({"JIN10_WEB_QUOTE_FREQUENCY_MS": "0"})
        self.assertEqual(settings.quote_frequency_ms, 0)

    def test_rejects_negative_frequency(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 0 and 60000"):
            Jin10WebSettings.from_env({"JIN10_WEB_QUOTE_FREQUENCY_MS": "-1"})


if __name__ == "__main__":
    unittest.main()
