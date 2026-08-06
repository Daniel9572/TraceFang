from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from market_analysis.infrastructure.providers.jin10 import SPOT_GOLD
from market_analysis.infrastructure.providers.jin10_local.protocol import (
    QUOTE_PUSH_PROTOCOL,
    Jin10WireQuote,
)
from market_analysis.infrastructure.providers.jin10_local.provider import Jin10LocalProvider
from market_analysis.infrastructure.providers.jin10_local.settings import Jin10LocalSettings


class Jin10LocalProviderTests(unittest.TestCase):
    def test_dispatches_decoded_quote_to_listener_synchronously(self) -> None:
        provider = Jin10LocalProvider(Jin10LocalSettings(session_token="s" * 36, user_id=1))
        received = []
        provider.add_quote_listener(received.append)

        provider._store_quote(
            Jin10WireQuote(
                provider_code="XAUUSD.GOODS",
                last_micros=4_252_340_000,
                buy_micros=4_252_340_000,
                ask_micros=4_252_440_000,
                volume=10,
                high_micros=4_300_000_000,
                open_micros=4_247_000_000,
                low_micros=4_240_000_000,
                previous_close_micros=4_246_000_000,
                turnover=1,
                timestamp=int(datetime.now(UTC).timestamp()),
            ),
            protocol=QUOTE_PUSH_PROTOCOL,
        )

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].last, Decimal("4252.340000"))

        provider.remove_quote_listener(received.append)
        self.assertEqual(len(provider._quote_listeners), 0)


class Jin10LocalCandleTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _wire_quote(last_micros: int, timestamp: datetime) -> Jin10WireQuote:
        return Jin10WireQuote(
            provider_code="XAUUSD.GOODS",
            last_micros=last_micros,
            buy_micros=last_micros,
            ask_micros=last_micros + 100_000,
            volume=10,
            high_micros=4_500_000_000,
            open_micros=4_247_000_000,
            low_micros=4_000_000_000,
            previous_close_micros=4_246_000_000,
            turnover=1,
            timestamp=int(timestamp.timestamp()),
        )

    async def test_builds_minute_candles_only_from_local_quotes(self) -> None:
        provider = Jin10LocalProvider(Jin10LocalSettings(session_token="s" * 36, user_id=1))
        first_minute = datetime.now(UTC).replace(second=5, microsecond=0)

        provider._store_quote(
            self._wire_quote(4_250_000_000, first_minute),
            protocol=QUOTE_PUSH_PROTOCOL,
        )
        provider._store_quote(
            self._wire_quote(4_252_000_000, first_minute + timedelta(seconds=10)),
            protocol=QUOTE_PUSH_PROTOCOL,
        )
        provider._store_quote(
            self._wire_quote(4_249_000_000, first_minute + timedelta(minutes=1)),
            protocol=QUOTE_PUSH_PROTOCOL,
        )

        candles = await provider.get_candles(SPOT_GOLD, count=10)

        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].open, Decimal("4250.000000"))
        self.assertEqual(candles[0].high, Decimal("4252.000000"))
        self.assertEqual(candles[0].low, Decimal("4250.000000"))
        self.assertEqual(candles[0].close, Decimal("4252.000000"))
        self.assertEqual(candles[0].source.provider, "jin10_local")
        self.assertEqual(candles[1].open, Decimal("4249.000000"))


if __name__ == "__main__":
    unittest.main()
