from __future__ import annotations

import gzip
import struct
import unittest

from tracefang.infrastructure.providers.jin10_local.protocol import (
    ADVANCED_QUOTE_REQUEST_PROTOCOL,
    KLINE_HISTORY_PROTOCOL,
    KLINE_SUBSCRIPTION_PROTOCOL,
    LOGIN_PROTOCOL,
    decode_message,
    derive_session_key,
    encode_kline_history_request,
    encode_kline_subscription,
    encode_login,
    encode_quote_subscription,
    parse_kline_history_file,
    parse_kline_history_manifest,
    parse_kline_snapshot,
    parse_quote,
    xor_cipher,
)


def encode_string(value: str) -> bytes:
    encoded = value.encode()
    return struct.pack("<H", len(encoded)) + encoded


def read_string(packet: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack_from("<H", packet, offset)[0]
    offset += 2
    return packet[offset : offset + length].decode(), offset + length


def wire_candle(timestamp: int, close_micros: int = 4_251_000_000) -> bytes:
    return struct.pack(
        "<qqqqqq",
        timestamp,
        4_252_000_000,
        4_250_000_000,
        4_249_000_000,
        close_micros,
        10,
    )


class Jin10LocalProtocolTests(unittest.TestCase):
    def test_derives_key_from_known_desktop_handshake(self) -> None:
        handshake = bytes.fromhex("a2060f004bbd2b0005cd65359b260900")
        self.assertEqual(derive_session_key(handshake), "895864069.2866507")

    def test_xor_cipher_is_symmetric(self) -> None:
        clear = b"structured market data"
        key = "895864069.2866507"
        self.assertEqual(xor_cipher(xor_cipher(clear, key), key), clear)

    def test_encodes_login_without_implicit_fields(self) -> None:
        token = "x" * 36
        packet = encode_login(user_id=8616672, session_token=token, vip_type=3)
        protocol, user_id = struct.unpack_from("<hi", packet)
        self.assertEqual(protocol, LOGIN_PROTOCOL)
        self.assertEqual(user_id, 8616672)
        value, offset = read_string(packet, 6)
        self.assertEqual(value, token)
        empty, offset = read_string(packet, offset)
        self.assertEqual(empty, "")
        self.assertEqual(struct.unpack_from("<i", packet, offset)[0], 3)

    def test_encodes_deduplicated_quote_subscription(self) -> None:
        packet = encode_quote_subscription(
            provider_codes=("XAUUSD.GOODS", "XAUUSD.GOODS", "XAGUSD.GOODS"),
            frequency_ms=3000,
        )
        protocol, frequency, count = struct.unpack_from("<hih", packet)
        self.assertEqual(protocol, ADVANCED_QUOTE_REQUEST_PROTOCOL)
        self.assertEqual(frequency, 3000)
        self.assertEqual(count, 2)

    def test_encodes_kline_subscription_with_per_symbol_time_type(self) -> None:
        packet = encode_kline_subscription(
            provider_codes=("XAUUSD.GOODS", "XAGUSD.GOODS"),
            frequency_ms=3000,
            time_type=1,
        )
        protocol, frequency, count = struct.unpack_from("<hih", packet)
        self.assertEqual(protocol, KLINE_SUBSCRIPTION_PROTOCOL)
        self.assertEqual(frequency, 3000)
        self.assertEqual(count, 2)
        first, offset = read_string(packet, 8)
        self.assertEqual(first, "XAUUSD.GOODS")
        self.assertEqual(struct.unpack_from("<h", packet, offset)[0], 1)

    def test_encodes_backward_history_request(self) -> None:
        packet = encode_kline_history_request(
            provider_code="XAUUSD.GOODS",
            time_type=1,
            boundary_timestamp=1_786_000_000,
        )
        self.assertEqual(struct.unpack_from("<h", packet)[0], KLINE_HISTORY_PROTOCOL)
        code, offset = read_string(packet, 2)
        self.assertEqual(code, "XAUUSD.GOODS")
        self.assertEqual(
            struct.unpack_from("<bqhb", packet, offset),
            (1, 1_786_000_000, 1, -1),
        )

    def test_parses_first_kline_snapshot_at_micro_price_precision(self) -> None:
        payload = (
            encode_string("XAUUSD.GOODS") + struct.pack("<bi", 1, 1) + wire_candle(1_786_027_380)
        )
        snapshot = parse_kline_snapshot(payload)
        self.assertEqual(snapshot.provider_code, "XAUUSD.GOODS")
        self.assertEqual(snapshot.time_type, 1)
        self.assertEqual(snapshot.candles[0].open_micros, 4_250_000_000)
        self.assertEqual(snapshot.candles[0].close_micros, 4_251_000_000)

    def test_parses_history_manifest_and_gzip_records(self) -> None:
        file_name = "25b57cce844256b11025c73a947753b4.2.1786027380.1786027440"
        payload = (
            encode_string("XAUUSD.GOODS")
            + struct.pack("<hbbqb", 0, 0, 1, -1, 1)
            + encode_string(file_name)
        )
        manifest = parse_kline_history_manifest(payload)
        self.assertEqual(manifest.boundary_timestamp, -1)
        self.assertEqual(manifest.files[0].record_count, 2)
        self.assertEqual(manifest.files[0].start_timestamp, 1_786_027_380)

        rows = parse_kline_history_file(
            gzip.compress(
                wire_candle(1_786_027_380) + wire_candle(1_786_027_440, close_micros=4_251_500_000)
            )
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].timestamp, 1_786_027_440)
        self.assertEqual(rows[1].close_micros, 4_251_500_000)

    def test_parses_observed_legacy_quote_frame(self) -> None:
        code = b"XAUUSD.GOODS"
        payload = (
            struct.pack("<H", len(code))
            + code
            + struct.pack(
                "<qqqqqqqqqi",
                4_256_230_000,
                4_256_230_000,
                4_256_280_000,
                75_486,
                4_304_080_000,
                4_247_780_000,
                4_245_750_000,
                4_246_730_000,
                0,
                1_785_997_733,
            )
        )
        quote = parse_quote(payload)
        self.assertEqual(quote.provider_code, "XAUUSD.GOODS")
        self.assertEqual(quote.last_micros, 4_256_230_000)
        self.assertEqual(quote.ask_micros, 4_256_280_000)
        self.assertEqual(quote.volume, 75_486)

    def test_decodes_message_protocol_and_payload(self) -> None:
        self.assertEqual(decode_message(struct.pack("<h", 10005) + b"quote"), (10005, b"quote"))

    def test_snapshot_rejects_invalid_ohlc_invariant(self) -> None:
        payload = (
            encode_string("XAUUSD.GOODS")
            + struct.pack("<bi", 1, 1)
            + struct.pack(
                "<qqqqqq",
                1_786_027_380,
                4_249_000_000,
                4_250_000_000,
                4_248_000_000,
                4_251_000_000,
                10,
            )
        )
        with self.assertRaisesRegex(Exception, "outside low/high"):
            parse_kline_snapshot(payload)


if __name__ == "__main__":
    unittest.main()
