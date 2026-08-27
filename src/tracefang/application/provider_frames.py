from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class ProviderFrame:
    version: int
    channel: str
    connection_id: str
    sequence: int
    received_at: datetime
    encoding: str
    body: bytes

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("provider frame version must be positive")
        for value, field in (
            (self.channel, "channel"),
            (self.connection_id, "connection_id"),
            (self.encoding, "encoding"),
        ):
            if not _TOKEN.fullmatch(value):
                raise ValueError(f"provider frame {field} must be a plain token")
        if self.sequence < 1:
            raise ValueError("provider frame sequence must be positive")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("provider frame received_at must be timezone-aware")
        if not isinstance(self.body, bytes):
            raise TypeError("provider frame body must be bytes")


class RawFrameSink(Protocol):
    async def capture(self, frame: ProviderFrame) -> int: ...
