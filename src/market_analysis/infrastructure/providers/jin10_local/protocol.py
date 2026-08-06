from __future__ import annotations

import struct
from dataclasses import dataclass

from market_analysis.domain.errors import ProviderDataError

LOGIN_PROTOCOL = 10018
ADVANCED_QUOTE_REQUEST_PROTOCOL = 10003
QUOTE_PUSH_PROTOCOL = 10005
ADVANCED_QUOTE_PUSH_PROTOCOL = 20010
RELOGIN_REQUEST_PROTOCOL = 21113

_QUOTE_CORE = struct.Struct("<qqqqqqqqqi")


@dataclass(frozen=True, slots=True)
class Jin10WireQuote:
    provider_code: str
    last_micros: int
    buy_micros: int
    ask_micros: int
    volume: int
    high_micros: int
    open_micros: int
    low_micros: int
    previous_close_micros: int
    turnover: int
    timestamp: int


def _encode_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise ValueError("Jin10 protocol string is too long")
    return struct.pack("<H", len(encoded)) + encoded


def derive_session_key(handshake: bytes) -> str:
    if len(handshake) < 12:
        raise ProviderDataError("Jin10 local handshake is truncated")
    _, second, third = struct.unpack_from("<III", handshake)
    key = f"{third}.{second}"
    if not key:
        raise ProviderDataError("Jin10 local handshake did not provide a session key")
    return key


def xor_cipher(data: bytes, key: str) -> bytes:
    key_bytes = key.encode("ascii")
    if not key_bytes:
        raise ValueError("Jin10 local cipher key cannot be empty")
    offset = key_bytes[0]
    return bytes(
        value ^ key_bytes[(index + offset) % len(key_bytes)] for index, value in enumerate(data)
    )


def encode_login(
    *,
    user_id: int,
    session_token: str,
    vip_type: int = 3,
) -> bytes:
    if not -(2**31) <= user_id < 2**31:
        raise ValueError("Jin10 local user id is outside int32 range")
    return b"".join(
        (
            struct.pack("<hi", LOGIN_PROTOCOL, user_id),
            _encode_string(session_token),
            _encode_string(""),
            struct.pack("<i", vip_type),
            _encode_string("web"),
            struct.pack("<h", 3),
        )
    )


def encode_quote_subscription(*, provider_codes: tuple[str, ...], frequency_ms: int) -> bytes:
    unique_codes = tuple(dict.fromkeys(provider_codes))
    if len(unique_codes) > 0x7FFF:
        raise ValueError("too many Jin10 local quote subscriptions")
    return b"".join(
        (
            struct.pack(
                "<hih",
                ADVANCED_QUOTE_REQUEST_PROTOCOL,
                frequency_ms,
                len(unique_codes),
            ),
            *(_encode_string(code) for code in unique_codes),
        )
    )


def decode_message(message: bytes) -> tuple[int, bytes]:
    if len(message) < 2:
        raise ProviderDataError("Jin10 local message is truncated")
    return struct.unpack_from("<h", message)[0], message[2:]


def parse_quote(payload: bytes) -> Jin10WireQuote:
    if len(payload) < 2:
        raise ProviderDataError("Jin10 local quote is missing its symbol")
    symbol_length = struct.unpack_from("<H", payload)[0]
    symbol_end = 2 + symbol_length
    if symbol_length == 0 or symbol_end > len(payload):
        raise ProviderDataError("Jin10 local quote symbol length is invalid")
    try:
        provider_code = payload[2:symbol_end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProviderDataError("Jin10 local quote symbol is not UTF-8") from error
    if len(payload) - symbol_end < _QUOTE_CORE.size:
        raise ProviderDataError("Jin10 local quote payload is truncated")
    values = _QUOTE_CORE.unpack_from(payload, symbol_end)
    return Jin10WireQuote(provider_code, *values)
