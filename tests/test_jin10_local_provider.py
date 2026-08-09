from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

from market_analysis.infrastructure.providers.jin10 import SPOT_GOLD
from market_analysis.infrastructure.providers.jin10_local.protocol import (
    KLINE_HISTORY_PROTOCOL,
    KLINE_UPDATE_PROTOCOL,
    QUOTE_PUSH_PROTOCOL,
    Jin10KlineHistoryManifest,
    Jin10KlineSnapshot,
    Jin10WireCandle,
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

    async def test_quotes_do_not_build_provider_owned_minute_candles(self) -> None:
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

        self.assertEqual(candles, ())

    async def test_dispatches_native_bar_updates_to_candle_listener(self) -> None:
        provider = Jin10LocalProvider(Jin10LocalSettings(session_token="s" * 36, user_id=1))
        received = []
        provider.add_candle_listener(received.append)
        timestamp = int(datetime.now(UTC).replace(second=0, microsecond=0).timestamp())

        provider._store_kline_snapshot(
            Jin10KlineSnapshot(
                provider_code="XAUUSD.GOODS",
                time_type=1,
                candles=(
                    Jin10WireCandle(
                        timestamp=timestamp,
                        high_micros=4_252_000_000,
                        open_micros=4_250_000_000,
                        low_micros=4_249_000_000,
                        close_micros=4_251_000_000,
                        volume=10,
                    ),
                ),
            ),
            protocol=KLINE_UPDATE_PROTOCOL,
        )

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].close, Decimal("4251.000000"))
        self.assertEqual(
            received[0].source.raw_payload["bar_state"],
            "provisional_authoritative",
        )

        provider.remove_candle_listener(received.append)
        self.assertEqual(len(provider._candle_listeners), 0)

    async def test_stores_exact_history_file_ohlcv_and_lineage(self) -> None:
        provider = Jin10LocalProvider(Jin10LocalSettings(session_token="s" * 36, user_id=1))
        timestamp = 1_786_027_380

        rows = provider._store_wire_candles(
            SPOT_GOLD,
            "XAUUSD.GOODS",
            (
                Jin10WireCandle(
                    timestamp=timestamp,
                    high_micros=4_274_819_999,
                    open_micros=4_273_470_000,
                    low_micros=4_273_380_000,
                    close_micros=4_273_560_000,
                    volume=189,
                ),
            ),
            protocol=KLINE_HISTORY_PROTOCOL,
            time_type=1,
            file_name="25b57cce844256b11025c73a947753b4",
        )

        self.assertEqual(rows[0].open, Decimal("4273.470000"))
        self.assertEqual(rows[0].high, Decimal("4274.819999"))
        self.assertEqual(rows[0].volume, Decimal("189"))
        self.assertEqual(rows[0].source.provider, "jin10_local")
        self.assertEqual(
            rows[0].source.raw_payload["history_file"],
            "25b57cce844256b11025c73a947753b4",
        )

    async def test_targets_history_request_at_the_missing_window_end(self) -> None:
        provider = Jin10LocalProvider(Jin10LocalSettings(session_token="s" * 36, user_id=1))
        start = datetime(2026, 8, 1, tzinfo=UTC)
        count = 2_880
        boundary = int((start + timedelta(minutes=count)).timestamp())
        provider.open = AsyncMock()
        provider._connection_ready.set()
        provider._request_history_manifest = AsyncMock(
            return_value=Jin10KlineHistoryManifest(
                provider_code="XAUUSD.GOODS",
                time_type=1,
                boundary_timestamp=boundary,
                files=(),
            )
        )

        candles = await provider.fetch_historical_candles(
            SPOT_GOLD,
            start=start,
            count=count,
        )

        self.assertEqual(candles, ())
        provider.open.assert_awaited_once()
        provider._request_history_manifest.assert_awaited_once_with(
            "XAUUSD.GOODS",
            time_type=1,
            boundary_timestamp=boundary,
        )

    async def test_history_fetch_does_not_promote_live_native_bar_to_final_history(self) -> None:
        provider = Jin10LocalProvider(Jin10LocalSettings(session_token="s" * 36, user_id=1))
        start = datetime(2026, 8, 1, tzinfo=UTC)
        provider._store_kline_snapshot(
            Jin10KlineSnapshot(
                provider_code="XAUUSD.GOODS",
                time_type=1,
                candles=(
                    Jin10WireCandle(
                        timestamp=int(start.timestamp()),
                        high_micros=4_252_000_000,
                        open_micros=4_250_000_000,
                        low_micros=4_249_000_000,
                        close_micros=4_251_000_000,
                        volume=10,
                    ),
                ),
            ),
            protocol=KLINE_UPDATE_PROTOCOL,
        )
        provider.open = AsyncMock()
        provider._connection_ready.set()
        provider._request_history_manifest = AsyncMock(
            return_value=Jin10KlineHistoryManifest(
                provider_code="XAUUSD.GOODS",
                time_type=1,
                boundary_timestamp=int((start + timedelta(minutes=1)).timestamp()),
                files=(),
            )
        )

        candles = await provider.fetch_historical_candles(
            SPOT_GOLD,
            start=start,
            count=1,
        )

        self.assertEqual(candles, ())


if __name__ == "__main__":
    unittest.main()
