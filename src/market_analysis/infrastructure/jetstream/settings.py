from __future__ import annotations

import os
import re
from dataclasses import dataclass

_STREAM_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_SUBJECT_PREFIX = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")


@dataclass(frozen=True, slots=True)
class JetStreamSettings:
    url: str
    stream_name: str = "MARKET_RAW_FRAMES"
    subject_prefix: str = "market.raw"
    connect_timeout_seconds: float = 2.0
    publish_timeout_seconds: float = 2.0
    max_age_seconds: float = 7 * 24 * 60 * 60
    max_bytes: int = 10 * 1024 * 1024 * 1024
    max_frame_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("JetStream URL must not be empty")
        if not _STREAM_NAME.fullmatch(self.stream_name):
            raise ValueError("JetStream stream name contains unsupported characters")
        if not _SUBJECT_PREFIX.fullmatch(self.subject_prefix):
            raise ValueError("JetStream subject prefix must contain plain subject tokens")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("JetStream connect timeout must be positive")
        if self.publish_timeout_seconds <= 0:
            raise ValueError("JetStream publish timeout must be positive")
        if self.max_age_seconds <= 0:
            raise ValueError("JetStream maximum age must be positive")
        if self.max_bytes <= 0:
            raise ValueError("JetStream maximum bytes must be positive")
        if self.max_frame_bytes <= 0:
            raise ValueError("JetStream maximum frame bytes must be positive")
        if self.max_frame_bytes > self.max_bytes:
            raise ValueError(
                "JetStream maximum frame bytes must not exceed the stream maximum bytes"
            )

    @property
    def capture_subject(self) -> str:
        return f"{self.subject_prefix}.>"

    def channel_subject(self, channel: str) -> str:
        if not _STREAM_NAME.fullmatch(channel):
            raise ValueError("frame channel must be one plain subject token")
        return f"{self.subject_prefix}.{channel}"

    @classmethod
    def from_env(cls) -> JetStreamSettings | None:
        url = os.environ.get("MARKET_ANALYSIS_NATS_URL", "").strip()
        if not url:
            return None
        return cls(
            url=url,
            stream_name=os.environ.get("MARKET_ANALYSIS_NATS_STREAM", "MARKET_RAW_FRAMES").strip(),
            subject_prefix=os.environ.get(
                "MARKET_ANALYSIS_NATS_SUBJECT_PREFIX", "market.raw"
            ).strip(),
            connect_timeout_seconds=float(
                os.environ.get("MARKET_ANALYSIS_NATS_CONNECT_TIMEOUT_SECONDS", "2")
            ),
            publish_timeout_seconds=float(
                os.environ.get("MARKET_ANALYSIS_NATS_PUBLISH_TIMEOUT_SECONDS", "2")
            ),
            max_age_seconds=float(os.environ.get("MARKET_ANALYSIS_NATS_MAX_AGE_SECONDS", "604800")),
            max_bytes=int(os.environ.get("MARKET_ANALYSIS_NATS_MAX_BYTES", "10737418240")),
            max_frame_bytes=int(
                os.environ.get("MARKET_ANALYSIS_NATS_MAX_FRAME_BYTES", "33554432")
            ),
        )
