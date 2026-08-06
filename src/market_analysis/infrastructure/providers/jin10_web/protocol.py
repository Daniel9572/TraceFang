from __future__ import annotations

import struct
from dataclasses import dataclass

from market_analysis.domain.errors import ProviderDataError

QUOTE_REQUEST_PROTOCOL = 10003
QUOTE_PUSH_PROTOCOL = 10005
SERVER_TIME_PROTOCOL = 1200

_QUOTE_CORE = struct.Struct("<Iqq")


@dataclass(frozen=True, slots=True)
class Jin10WebWireQuote:
    provider_code: str
    timestamp: int
    last_micros: int
    previous_close_micros: int


def _encode_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise ValueError("Jin10 web protocol string is too long")
    return struct.pack("<H", len(encoded)) + encoded


def encode_quote_subscription(*, provider_codes: tuple[str, ...], frequency_ms: int) -> bytes:
    unique_codes = tuple(dict.fromkeys(provider_codes))
    if len(unique_codes) > 0xFFFF:
        raise ValueError("too many Jin10 web quote subscriptions")
    return b"".join(
        (
            struct.pack("<HIH", QUOTE_REQUEST_PROTOCOL, frequency_ms, len(unique_codes)),
            *(_encode_string(code) for code in unique_codes),
        )
    )


def decode_message(message: bytes) -> tuple[int, bytes]:
    if len(message) < 2:
        raise ProviderDataError("Jin10 web message is truncated")
    return struct.unpack_from("<H", message)[0], message[2:]


def parse_quote(payload: bytes) -> Jin10WebWireQuote:
    if len(payload) < 2:
        raise ProviderDataError("Jin10 web quote is missing its symbol")
    symbol_length = struct.unpack_from("<H", payload)[0]
    symbol_end = 2 + symbol_length
    if symbol_length == 0 or symbol_end > len(payload):
        raise ProviderDataError("Jin10 web quote symbol length is invalid")
    try:
        provider_code = payload[2:symbol_end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProviderDataError("Jin10 web quote symbol is not UTF-8") from error
    if len(payload) - symbol_end < _QUOTE_CORE.size:
        raise ProviderDataError("Jin10 web quote payload is truncated")
    timestamp, last_micros, previous_close_micros = _QUOTE_CORE.unpack_from(
        payload,
        symbol_end,
    )
    return Jin10WebWireQuote(
        provider_code=provider_code,
        timestamp=timestamp,
        last_micros=last_micros,
        previous_close_micros=previous_close_micros,
    )
