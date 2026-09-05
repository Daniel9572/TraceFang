from __future__ import annotations

import gzip
import struct
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import httpx

from tracefang.application.provider_frames import ProviderFrame
from tracefang.domain.errors import ProviderDataError, ProviderUnavailableError
from tracefang.infrastructure.providers.jin10 import SPOT_GOLD
from tracefang.infrastructure.providers.jin10_local.protocol import (
    KLINE_HISTORY_PROTOCOL,
    KLINE_UPDATE_PROTOCOL,
    QUOTE_PUSH_PROTOCOL,
    RELOGIN_REQUEST_PROTOCOL,
    Jin10KlineHistoryFile,
    Jin10KlineHistoryManifest,
    Jin10KlineSnapshot,
    Jin10WireCandle,
    Jin10WireQuote,
    xor_cipher,
)
from tracefang.infrastructure.providers.jin10_local.provider import Jin10LocalProvider
from tracefang.infrastructure.providers.jin10_local.session import Jin10SessionCredentials
from tracefang.infrastructure.providers.jin10_local.settings import Jin10LocalSettings


class Jin10LocalProviderTests(unittest.TestCase):
    def test_health_distinguishes_expired_session_without_credentials(self) -> None:
        provider = Jin10LocalProvider(
            Jin10LocalSettings.for_credentials(session_token="s" * 36)
        )
        provider._task = Mock()
        provider._task.done.return_value = False
        provider._authentication_failed = True

        available, state, detail = provider.health()

        self.assertFalse(available)
        self.assertEqual(state, "authentication_failed")
        self.assertNotIn("s" * 36, detail or "")
        self.assertNotIn("user_id", detail or "")

    def test_dispatches_decoded_quote_to_listener_synchronously(self) -> None:
        provider = Jin10LocalProvider(
            Jin10LocalSettings.for_credentials(session_token="s" * 36)
        )
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
    def _history_payload(*timestamps: int) -> bytes:
        return gzip.compress(
            b"".join(
                struct.pack(
                    "<qqqqqq",
                    timestamp,
                    4_252_000_000,
                    4_250_000_000,
                    4_249_000_000,
                    4_251_000_000,
                    10,
                )
                for timestamp in timestamps
            )
        )

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

    async def test_login_resolves_a_fresh_credential_snapshot(self) -> None:
        resolver = Mock()
        resolver.resolve.return_value = Jin10SessionCredentials(
            session_token="r" * 36,
            origin="desktop",
        )
        settings = Jin10LocalSettings(session_resolver=resolver)
        provider = Jin10LocalProvider(settings)
        socket = AsyncMock()

        await provider._send_login(socket, "key", refresh=True)

        resolver.resolve.assert_called_once_with(refresh=True)
        socket.send.assert_awaited_once()
        login_packet = xor_cipher(socket.send.await_args.args[0], "key")
        _, user_id = struct.unpack_from("<hi", login_packet)
        self.assertEqual(user_id, 0)

    async def test_relogin_refreshes_once_then_reconnects(self) -> None:
        resolver = Mock()
        resolver.resolve.return_value = Jin10SessionCredentials(
            session_token="r" * 36,
            origin="desktop",
        )
        settings = Jin10LocalSettings(session_resolver=resolver)
        provider = Jin10LocalProvider(settings)
        socket = AsyncMock()
        frame = ProviderFrame(
            version=1,
            channel=provider.name,
            connection_id="test",
            sequence=1,
            received_at=datetime.now(UTC),
            encoding="session-decrypted",
            body=struct.pack("<h", RELOGIN_REQUEST_PROTOCOL),
        )

        await provider.ingest_frame(frame, socket=socket, session_key="key")

        resolver.resolve.assert_called_once_with(refresh=True)
        with self.assertRaisesRegex(ProviderUnavailableError, "credential refresh"):
            await provider.ingest_frame(frame, socket=socket, session_key="key")

    async def test_quotes_do_not_build_provider_owned_minute_candles(self) -> None:
        provider = Jin10LocalProvider(
            Jin10LocalSettings.for_credentials(session_token="s" * 36)
        )
        first_minute = datetime.now(UTC).replace(second=5, microsecond=0)

        provider._store_quote(
            self._wire_quote(4_250_000_000, first_minute),
            protocol=QUOTE_PUSH_PROTOCOL,
        )
        self.assertEqual(
            provider._latest["XAUUSD.GOODS"].source.raw_payload["observation_kind"],
            "event",
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
        provider = Jin10LocalProvider(
            Jin10LocalSettings.for_credentials(session_token="s" * 36)
        )
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
        provider = Jin10LocalProvider(
            Jin10LocalSettings.for_credentials(session_token="s" * 36)
        )
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
        provider = Jin10LocalProvider(
            Jin10LocalSettings.for_credentials(session_token="s" * 36)
        )
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

        batch = await provider.fetch_historical_candles(
            SPOT_GOLD,
            start=start,
            count=count,
        )

        self.assertEqual(batch.candles, ())
        self.assertEqual(batch.checked_start, start)
        self.assertEqual(batch.checked_end, start + timedelta(minutes=count))
        self.assertEqual(batch.authoritative_through, batch.checked_end)
        self.assertTrue(batch.evidence_version)
        self.assertIsNone(batch.history_floor)
        provider.open.assert_awaited_once()
        provider._request_history_manifest.assert_awaited_once_with(
            "XAUUSD.GOODS",
            time_type=1,
            boundary_timestamp=boundary,
        )

    async def test_history_file_accepts_append_only_rows_beyond_manifest_end(self) -> None:
        provider = Jin10LocalProvider(
            Jin10LocalSettings.for_credentials(session_token="s" * 36)
        )
        start = 1_786_027_380
        item = Jin10KlineHistoryFile(
            "mutable-history-file",
            record_count=2,
            start_timestamp=start,
            end_timestamp=start + 60,
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=self._history_payload(start, start + 60, start + 120),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            rows = await provider._download_history_file(
                client,
                SPOT_GOLD,
                "XAUUSD.GOODS",
                1,
                item,
            )

        self.assertEqual(len(rows), 3)
        self.assertEqual(requests[0].url.params["manifest_version"], f"2-{start + 60}")

    async def test_history_file_refreshes_a_cached_snapshot_behind_manifest(self) -> None:
        provider = Jin10LocalProvider(
            Jin10LocalSettings.for_credentials(session_token="s" * 36)
        )
        start = 1_786_027_380
        item = Jin10KlineHistoryFile(
            "mutable-history-file",
            record_count=2,
            start_timestamp=start,
            end_timestamp=start + 60,
        )
        cache_key = ("XAUUSD.GOODS", 1, item.file_name)
        provider._history_file_cache[cache_key] = provider._store_wire_candles(
            SPOT_GOLD,
            "XAUUSD.GOODS",
            (
                Jin10WireCandle(
                    timestamp=start,
                    high_micros=4_252_000_000,
                    open_micros=4_250_000_000,
                    low_micros=4_249_000_000,
                    close_micros=4_251_000_000,
                    volume=10,
                ),
            ),
            protocol=KLINE_HISTORY_PROTOCOL,
            time_type=1,
            file_name=item.file_name,
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=self._history_payload(start, start + 60),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            rows = await provider._download_history_file(
                client,
                SPOT_GOLD,
                "XAUUSD.GOODS",
                1,
                item,
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(len(rows), 2)

    async def test_history_file_retries_one_stale_manifest_version_download(self) -> None:
        provider = Jin10LocalProvider(
            Jin10LocalSettings.for_credentials(session_token="s" * 36)
        )
        start = 1_786_027_380
        item = Jin10KlineHistoryFile(
            "mutable-history-file",
            record_count=2,
            start_timestamp=start,
            end_timestamp=start + 60,
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            timestamps = (start,) if len(requests) == 1 else (start, start + 60)
            return httpx.Response(200, content=self._history_payload(*timestamps))

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            rows = await provider._download_history_file(
                client,
                SPOT_GOLD,
                "XAUUSD.GOODS",
                1,
                item,
            )

        self.assertEqual(len(requests), 2)
        self.assertNotIn("refresh", requests[0].url.params)
        self.assertIn("refresh", requests[1].url.params)
        self.assertEqual(len(rows), 2)

    async def test_history_file_rejects_rows_missing_inside_manifest_window(self) -> None:
        provider = Jin10LocalProvider(
            Jin10LocalSettings.for_credentials(session_token="s" * 36)
        )
        start = 1_786_027_380
        item = Jin10KlineHistoryFile(
            "truncated-history-file",
            record_count=2,
            start_timestamp=start,
            end_timestamp=start + 60,
        )

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=self._history_payload(start))

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaisesRegex(ProviderDataError, "expected=2, actual=1"):
                await provider._download_history_file(
                    client,
                    SPOT_GOLD,
                    "XAUUSD.GOODS",
                    1,
                    item,
                )

    async def test_history_fetch_does_not_promote_live_native_bar_to_final_history(self) -> None:
        provider = Jin10LocalProvider(
            Jin10LocalSettings.for_credentials(session_token="s" * 36)
        )
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

        batch = await provider.fetch_historical_candles(
            SPOT_GOLD,
            start=start,
            count=1,
        )

        self.assertEqual(batch.candles, ())


if __name__ == "__main__":
    unittest.main()
