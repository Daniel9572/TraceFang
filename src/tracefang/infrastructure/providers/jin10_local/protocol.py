from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass

from tracefang.domain.errors import ProviderDataError

LOGIN_PROTOCOL = 10018
KLINE_SUBSCRIPTION_PROTOCOL = 10002
KLINE_SNAPSHOT_PROTOCOL = 10004
KLINE_HISTORY_PROTOCOL = 10006
KLINE_UPDATE_PROTOCOL = 10007
ADVANCED_QUOTE_REQUEST_PROTOCOL = 10003
QUOTE_PUSH_PROTOCOL = 10005
ADVANCED_QUOTE_PUSH_PROTOCOL = 20010
RELOGIN_REQUEST_PROTOCOL = 21113

_QUOTE_CORE = struct.Struct("<qqqqqqqqqi")
_KLINE_CORE = struct.Struct("<qqqqqq")


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


@dataclass(frozen=True, slots=True)
class Jin10WireCandle:
    timestamp: int
    high_micros: int
    open_micros: int
    low_micros: int
    close_micros: int
    volume: int


@dataclass(frozen=True, slots=True)
class Jin10KlineSnapshot:
    provider_code: str
    time_type: int
    candles: tuple[Jin10WireCandle, ...]


@dataclass(frozen=True, slots=True)
class Jin10KlineHistoryFile:
    file_name: str
    record_count: int | None
    start_timestamp: int | None
    end_timestamp: int | None


@dataclass(frozen=True, slots=True)
class Jin10KlineHistoryManifest:
    provider_code: str
    time_type: int
    boundary_timestamp: int
    files: tuple[Jin10KlineHistoryFile, ...]


def _encode_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise ValueError("Jin10 protocol string is too long")
    return struct.pack("<H", len(encoded)) + encoded


def _decode_string(payload: bytes, offset: int = 0) -> tuple[str, int]:
    if len(payload) - offset < 2:
        raise ProviderDataError("Jin10 protocol string length is truncated")
    length = struct.unpack_from("<H", payload, offset)[0]
    start = offset + 2
    end = start + length
    if length == 0 or end > len(payload):
        raise ProviderDataError("Jin10 protocol string length is invalid")
    try:
        return payload[start:end].decode("utf-8"), end
    except UnicodeDecodeError as error:
        raise ProviderDataError("Jin10 protocol string is not UTF-8") from error


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


def encode_kline_subscription(
    *,
    provider_codes: tuple[str, ...],
    time_type: int = 1,
    frequency_ms: int = 3000,
) -> bytes:
    unique_codes = tuple(dict.fromkeys(provider_codes))
    if not 0 <= time_type <= 0x7FFF:
        raise ValueError("Jin10 Kline time type is outside int16 range")
    if len(unique_codes) > 0x7FFF:
        raise ValueError("too many Jin10 Kline subscriptions")
    return b"".join(
        (
            struct.pack("<hih", KLINE_SUBSCRIPTION_PROTOCOL, frequency_ms, len(unique_codes)),
            *(
                b"".join((_encode_string(code), struct.pack("<h", time_type)))
                for code in unique_codes
            ),
        )
    )


def encode_kline_history_request(
    *,
    provider_code: str,
    time_type: int = 1,
    boundary_timestamp: int = -1,
    direction: int = 1,
) -> bytes:
    if not -128 <= time_type <= 127:
        raise ValueError("Jin10 Kline time type is outside int8 range")
    if not -(2**63) <= boundary_timestamp < 2**63:
        raise ValueError("Jin10 Kline boundary is outside int64 range")
    if not -(2**15) <= direction < 2**15:
        raise ValueError("Jin10 Kline direction is outside int16 range")
    return b"".join(
        (
            struct.pack("<h", KLINE_HISTORY_PROTOCOL),
            _encode_string(provider_code),
            struct.pack("<bqhb", time_type, boundary_timestamp, direction, -1),
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


def _parse_wire_candle(payload: bytes, offset: int) -> tuple[Jin10WireCandle, int]:
    if len(payload) - offset < _KLINE_CORE.size:
        raise ProviderDataError("Jin10 Kline record is truncated")
    values = _KLINE_CORE.unpack_from(payload, offset)
    candle = Jin10WireCandle(*values)
    _validate_wire_candle(candle)
    return candle, offset + _KLINE_CORE.size


def _validate_wire_candle(candle: Jin10WireCandle) -> None:
    if candle.timestamp <= 0:
        raise ProviderDataError("Jin10 Kline timestamp must be positive")
    if (
        min(
            candle.high_micros,
            candle.open_micros,
            candle.low_micros,
            candle.close_micros,
        )
        <= 0
    ):
        raise ProviderDataError("Jin10 Kline prices must be positive")
    if not candle.low_micros <= candle.open_micros <= candle.high_micros:
        raise ProviderDataError("Jin10 Kline open is outside low/high")
    if not candle.low_micros <= candle.close_micros <= candle.high_micros:
        raise ProviderDataError("Jin10 Kline close is outside low/high")
    if candle.volume < 0:
        raise ProviderDataError("Jin10 Kline volume cannot be negative")


def parse_kline_snapshot(payload: bytes) -> Jin10KlineSnapshot:
    provider_code, offset = _decode_string(payload)
    if len(payload) - offset < 5:
        raise ProviderDataError("Jin10 first Kline payload is truncated")
    time_type = struct.unpack_from("<b", payload, offset)[0]
    count = struct.unpack_from("<i", payload, offset + 1)[0]
    if count < 0 or count > 100_000:
        raise ProviderDataError("Jin10 first Kline count is invalid")
    offset += 5
    candles: list[Jin10WireCandle] = []
    for _ in range(count):
        candle, offset = _parse_wire_candle(payload, offset)
        candles.append(candle)
    if offset != len(payload):
        raise ProviderDataError("Jin10 first Kline payload has trailing bytes")
    return Jin10KlineSnapshot(provider_code, time_type, tuple(candles))


def parse_kline_update(payload: bytes) -> Jin10KlineSnapshot:
    provider_code, offset = _decode_string(payload)
    if len(payload) - offset < 4:
        raise ProviderDataError("Jin10 live Kline payload is truncated")
    time_type = struct.unpack_from("<i", payload, offset)[0]
    candle, offset = _parse_wire_candle(payload, offset + 4)
    if offset != len(payload):
        raise ProviderDataError("Jin10 live Kline payload has trailing bytes")
    return Jin10KlineSnapshot(provider_code, time_type, (candle,))


def _parse_history_file(value: str) -> Jin10KlineHistoryFile:
    parts = value.split(".")
    file_name = parts[0].strip()
    if not file_name:
        raise ProviderDataError("Jin10 Kline history file name is empty")
    try:
        record_count = int(parts[1]) if len(parts) > 1 else None
        start_timestamp = int(parts[2]) if len(parts) > 2 else None
        end_timestamp = int(parts[3]) if len(parts) > 3 else None
    except ValueError as error:
        raise ProviderDataError("Jin10 Kline history file metadata is invalid") from error
    if record_count is not None and record_count < 0:
        raise ProviderDataError("Jin10 Kline history record count cannot be negative")
    return Jin10KlineHistoryFile(file_name, record_count, start_timestamp, end_timestamp)


def parse_kline_history_manifest(payload: bytes) -> Jin10KlineHistoryManifest:
    provider_code, offset = _decode_string(payload)
    if len(payload) - offset < 13:
        raise ProviderDataError("Jin10 Kline history manifest is truncated")
    offset += 2  # reserved int16
    offset += 1  # reserved int8
    time_type = struct.unpack_from("<b", payload, offset)[0]
    boundary_timestamp = struct.unpack_from("<q", payload, offset + 1)[0]
    file_count = struct.unpack_from("<b", payload, offset + 9)[0]
    if file_count < 0:
        raise ProviderDataError("Jin10 Kline history file count is invalid")
    offset += 10
    files: list[Jin10KlineHistoryFile] = []
    for _ in range(file_count):
        value, offset = _decode_string(payload, offset)
        files.append(_parse_history_file(value))
    if offset != len(payload):
        raise ProviderDataError("Jin10 Kline history manifest has trailing bytes")
    return Jin10KlineHistoryManifest(
        provider_code,
        time_type,
        boundary_timestamp,
        tuple(files),
    )


def parse_kline_history_file(payload: bytes) -> tuple[Jin10WireCandle, ...]:
    try:
        decoded = gzip.decompress(payload)
    except (EOFError, OSError) as error:
        raise ProviderDataError("Jin10 Kline history file is not valid GZip data") from error
    if not decoded or len(decoded) % _KLINE_CORE.size:
        raise ProviderDataError("Jin10 Kline history file has an invalid record length")
    candles: list[Jin10WireCandle] = []
    for offset in range(0, len(decoded), _KLINE_CORE.size):
        candle, _ = _parse_wire_candle(decoded, offset)
        candles.append(candle)
    return tuple(candles)
