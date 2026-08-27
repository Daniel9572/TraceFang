from __future__ import annotations

import asyncio
import os
import time
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from market_analysis.infrastructure.jetstream import FrameEnvelope, FrameStore, JetStreamSettings

NATS_TEST_URL = os.environ.get("MARKET_ANALYSIS_TEST_NATS_URL", "").strip()


@unittest.skipUnless(NATS_TEST_URL, "MARKET_ANALYSIS_TEST_NATS_URL is not configured")
class JetStreamReplayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Black-box contract against a real nats-server with JetStream enabled."""

    async def asyncSetUp(self) -> None:
        token = uuid4().hex
        self.settings = JetStreamSettings(
            NATS_TEST_URL,
            stream_name=f"RAW_TEST_{token}",
            subject_prefix=f"test.raw.{token}",
            max_age_seconds=60,
            max_bytes=1024 * 1024,
            max_frame_bytes=512 * 1024,
        )
        self.store = FrameStore(self.settings)
        await self.store.connect()

    async def asyncTearDown(self) -> None:
        if self.store.is_connected:
            jetstream = self.store._require_jetstream()
            await jetstream.delete_stream(self.settings.stream_name)
        await self.store.close()

    @staticmethod
    def _frame(sequence: int) -> FrameEnvelope:
        return FrameEnvelope(
            version=1,
            channel="integration",
            connection_id="capture-1",
            sequence=sequence,
            received_at=datetime.now(UTC),
            encoding="wire",
            body=f"frame-{sequence}".encode(),
        )

    async def test_capture_restart_and_replay_original_preserve_order_and_cadence(self) -> None:
        first = await self.store.capture(self._frame(1))
        await asyncio.sleep(0.12)
        second = await self.store.capture(self._frame(2))
        await asyncio.sleep(0.18)
        third = await self.store.capture(self._frame(3))
        self.assertEqual((second, third), (first + 1, first + 2))

        await self.store.close()
        self.store = FrameStore(self.settings)
        await self.store.connect()
        bounds = await self.store.bounds()
        self.assertEqual(
            (bounds.first_sequence, bounds.last_sequence, bounds.message_count),
            (first, third, 3),
        )

        observed: list[tuple[int, bytes, float]] = []
        started = time.perf_counter()

        async def accept(recorded) -> None:
            observed.append(
                (
                    recorded.stream_sequence,
                    recorded.envelope.body,
                    time.perf_counter() - started,
                )
            )

        session = await self.store.replay(
            start_sequence=first,
            end_sequence=third,
            on_frame=accept,
        )
        await asyncio.wait_for(session.wait(), timeout=3)

        self.assertEqual(
            [(sequence, body) for sequence, body, _ in observed],
            [
                (first, b"frame-1"),
                (second, b"frame-2"),
                (third, b"frame-3"),
            ],
        )
        # JetStream, not application sleeps, owns these recorded intervals.
        self.assertGreaterEqual(observed[1][2] - observed[0][2], 0.08)
        self.assertGreaterEqual(observed[2][2] - observed[1][2], 0.13)
        info = await self.store._require_jetstream().stream_info(self.settings.stream_name)
        self.assertEqual(info.state.consumer_count, 0)


if __name__ == "__main__":
    unittest.main()
