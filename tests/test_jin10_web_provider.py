from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from market_analysis.infrastructure.providers.jin10_web.protocol import (
    QUOTE_PUSH_PROTOCOL,
    Jin10WebWireQuote,
)
from market_analysis.infrastructure.providers.jin10_web.provider import Jin10WebProvider
from market_analysis.infrastructure.providers.jin10_web.settings import Jin10WebSettings


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
        self.assertEqual(received[1].change, Decimal("6.360000"))

        provider.remove_quote_listener(received.append)
        self.assertEqual(len(provider._quote_listeners), 0)


class Jin10WebSettingsTests(unittest.TestCase):
    def test_accepts_change_driven_zero_frequency(self) -> None:
        settings = Jin10WebSettings.from_env({"JIN10_WEB_QUOTE_FREQUENCY_MS": "0"})
        self.assertEqual(settings.quote_frequency_ms, 0)

    def test_rejects_negative_frequency(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 0 and 60000"):
            Jin10WebSettings.from_env({"JIN10_WEB_QUOTE_FREQUENCY_MS": "-1"})


if __name__ == "__main__":
    unittest.main()
