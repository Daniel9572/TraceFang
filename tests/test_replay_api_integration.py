from __future__ import annotations

import asyncio
import json
import os
import unittest
from urllib.request import urlopen

import websockets

API_TEST_URL = os.environ.get("MARKET_ANALYSIS_TEST_API_URL", "").rstrip("/")
API_TEST_CODE = os.environ.get("MARKET_ANALYSIS_TEST_REPLAY_CODE", "XAUUSD").strip()


@unittest.skipUnless(API_TEST_URL, "MARKET_ANALYSIS_TEST_API_URL is not configured")
class ReplayApiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _bounds(self) -> dict[str, object]:
        def read_bounds() -> dict[str, object]:
            with urlopen(f"{API_TEST_URL}/api/replay/frames", timeout=5) as response:
                return json.load(response)

        return await asyncio.to_thread(read_bounds)

    async def test_websocket_rebuilds_from_first_retained_frame_at_original_rate(self) -> None:
        bounds = await self._bounds()
        self.assertEqual(bounds["state"], "ready")
        first = int(bounds["first_sequence"])
        # The endpoint test needs several complete seconds, not the entire
        # continuously growing retention window. Cadence itself is measured by
        # the dedicated temporary-stream integration test.
        last = min(int(bounds["last_sequence"]), first + 49)
        websocket_base = API_TEST_URL.replace("http://", "ws://").replace("https://", "wss://")
        url = (
            f"{websocket_base}/api/replay/stream/{API_TEST_CODE}"
            f"?period=1s&start_sequence={first}&end_sequence={last}"
        )

        kinds: set[str] = set()
        decode_errors: list[str] = []
        async with websockets.connect(url, open_timeout=5) as socket:
            initial = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            self.assertEqual(initial["kind"], "status")
            self.assertEqual(initial["state"], "playing")
            self.assertEqual(initial["replay_policy"], "original")
            self.assertNotIn("speed", initial)
            self.assertEqual(initial["start_sequence"], first)
            self.assertEqual(initial["end_sequence"], last)

            while True:
                event = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
                kinds.add(event["kind"])
                if event["kind"] == "decode_error":
                    decode_errors.append(str(event.get("error")))
                if event["kind"] == "status" and event.get("state") == "completed":
                    break

        self.assertIn("frame", kinds)
        self.assertIn("quote", kinds, decode_errors[:5])
        self.assertIn("bar", kinds, decode_errors[:5])

    async def test_websocket_rejects_arbitrary_seek_without_a_checkpoint(self) -> None:
        bounds = await self._bounds()
        first = int(bounds["first_sequence"])
        websocket_base = API_TEST_URL.replace("http://", "ws://").replace("https://", "wss://")
        url = (
            f"{websocket_base}/api/replay/stream/{API_TEST_CODE}"
            f"?period=1s&start_sequence={first + 1}&end_sequence={first + 2}"
        )

        async with websockets.connect(url, open_timeout=5) as socket:
            event = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))

        self.assertEqual(event["kind"], "status")
        self.assertEqual(event["state"], "unavailable")
        self.assertIn("first retained frame", event["error"])


if __name__ == "__main__":
    unittest.main()
