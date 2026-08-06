from __future__ import annotations

import struct
import unittest

from market_analysis.infrastructure.providers.jin10_local.protocol import (
    ADVANCED_QUOTE_REQUEST_PROTOCOL,
    LOGIN_PROTOCOL,
    decode_message,
    derive_session_key,
    encode_login,
    encode_quote_subscription,
    parse_quote,
    xor_cipher,
)


def read_string(packet: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack_from("<H", packet, offset)[0]
    offset += 2
    return packet[offset : offset + length].decode(), offset + length


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


if __name__ == "__main__":
    unittest.main()
