from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
_HEADER_VERSION = "Market-Frame-Version"
_HEADER_CHANNEL = "Market-Frame-Channel"
_HEADER_CONNECTION = "Market-Frame-Connection"
_HEADER_SEQUENCE = "Market-Frame-Sequence"
_HEADER_RECEIVED_AT = "Market-Frame-Received-At"
_HEADER_ENCODING = "Market-Frame-Encoding"
_HEADER_MESSAGE_ID = "Nats-Msg-Id"


def _plain_token(value: str, field: str) -> None:
    if not _TOKEN.fullmatch(value):
        raise ValueError(f"{field} must be a non-empty plain token")


@dataclass(frozen=True, slots=True)
class FrameEnvelope:
    version: int
    channel: str
    connection_id: str
    sequence: int
    received_at: datetime
    encoding: str
    body: bytes

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("frame version must be positive")
        _plain_token(self.channel, "frame channel")
        _plain_token(self.connection_id, "frame connection ID")
        if self.sequence < 1:
            raise ValueError("frame sequence must be positive")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("frame received_at must be timezone-aware")
        _plain_token(self.encoding, "frame encoding")
        if not isinstance(self.body, bytes):
            raise TypeError("frame body must be bytes")

    def headers(self) -> dict[str, str]:
        received_at = self.received_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return {
            _HEADER_VERSION: str(self.version),
            _HEADER_CHANNEL: self.channel,
            _HEADER_CONNECTION: self.connection_id,
            _HEADER_SEQUENCE: str(self.sequence),
            _HEADER_RECEIVED_AT: received_at,
            _HEADER_ENCODING: self.encoding,
            _HEADER_MESSAGE_ID: f"{self.channel}:{self.connection_id}:{self.sequence}",
        }

    @classmethod
    def from_message(
        cls,
        body: bytes,
        headers: Mapping[str, str] | None,
    ) -> FrameEnvelope:
        values = headers or {}
        required = (
            _HEADER_VERSION,
            _HEADER_CHANNEL,
            _HEADER_CONNECTION,
            _HEADER_SEQUENCE,
            _HEADER_RECEIVED_AT,
            _HEADER_ENCODING,
        )
        missing = [key for key in required if key not in values]
        if missing:
            raise ValueError(f"recorded frame is missing headers: {', '.join(missing)}")
        received_at = datetime.fromisoformat(values[_HEADER_RECEIVED_AT].replace("Z", "+00:00"))
        return cls(
            version=int(values[_HEADER_VERSION]),
            channel=values[_HEADER_CHANNEL],
            connection_id=values[_HEADER_CONNECTION],
            sequence=int(values[_HEADER_SEQUENCE]),
            received_at=received_at,
            encoding=values[_HEADER_ENCODING],
            body=body,
        )
