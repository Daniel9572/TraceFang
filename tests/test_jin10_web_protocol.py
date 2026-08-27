from __future__ import annotations

import struct
import unittest

from tracefang.infrastructure.providers.jin10_web.protocol import (
    QUOTE_REQUEST_PROTOCOL,
    decode_message,
    encode_quote_subscription,
    parse_quote,
)


def read_string(packet: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack_from("<H", packet, offset)[0]
    offset += 2
    return packet[offset : offset + length].decode(), offset + length


class Jin10WebProtocolTests(unittest.TestCase):
    def test_encodes_zero_frequency_subscription_without_duplicate_codes(self) -> None:
        packet = encode_quote_subscription(
            provider_codes=("XAUUSD.GOODS", "XAUUSD.GOODS", "XAGUSD.GOODS"),
            frequency_ms=0,
        )

        protocol, frequency, count = struct.unpack_from("<HIH", packet)
        self.assertEqual(protocol, QUOTE_REQUEST_PROTOCOL)
        self.assertEqual(frequency, 0)
        self.assertEqual(count, 2)
        first, offset = read_string(packet, 8)
        second, _ = read_string(packet, offset)
        self.assertEqual((first, second), ("XAUUSD.GOODS", "XAGUSD.GOODS"))

    def test_parses_public_quote_frame(self) -> None:
        code = b"XAUUSD.GOODS"
        payload = (
            struct.pack("<H", len(code))
            + code
            + struct.pack("<Iqq", 1_786_016_195, 4_266_530_000, 4_246_730_000)
        )

        quote = parse_quote(payload)

        self.assertEqual(quote.provider_code, "XAUUSD.GOODS")
        self.assertEqual(quote.timestamp, 1_786_016_195)
        self.assertEqual(quote.last_micros, 4_266_530_000)
        self.assertEqual(quote.previous_close_micros, 4_246_730_000)

    def test_decodes_unsigned_protocol_and_payload(self) -> None:
        self.assertEqual(
            decode_message(struct.pack("<H", 10005) + b"quote"),
            (10005, b"quote"),
        )


if __name__ == "__main__":
    unittest.main()
