from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_JIN10_WEB_URL = "wss://b-price.jin10.com/"
DEFAULT_JIN10_WEB_ORIGIN = "https://www.jin10.com"


@dataclass(frozen=True, slots=True)
class Jin10WebSettings:
    endpoint: str = DEFAULT_JIN10_WEB_URL
    origin: str = DEFAULT_JIN10_WEB_ORIGIN
    quote_frequency_ms: int = 0
    quote_wait_timeout_seconds: float = 5.0
    stale_after_seconds: float = 12.0
    connect_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Jin10WebSettings:
        values = os.environ if env is None else env
        frequency_ms = int(values.get("JIN10_WEB_QUOTE_FREQUENCY_MS", "0"))
        wait_timeout = float(values.get("JIN10_WEB_QUOTE_WAIT_TIMEOUT_SECONDS", "5"))
        stale_after = float(values.get("JIN10_WEB_STALE_AFTER_SECONDS", "12"))
        connect_timeout = float(values.get("JIN10_WEB_CONNECT_TIMEOUT_SECONDS", "10"))
        if not 0 <= frequency_ms <= 60_000:
            raise ValueError("JIN10_WEB_QUOTE_FREQUENCY_MS must be between 0 and 60000")
        if min(wait_timeout, stale_after, connect_timeout) <= 0:
            raise ValueError("Jin10 web timeout values must be positive")
        endpoint = values.get("JIN10_WEB_URL", DEFAULT_JIN10_WEB_URL).strip()
        origin = values.get("JIN10_WEB_ORIGIN", DEFAULT_JIN10_WEB_ORIGIN).strip()
        if not endpoint:
            raise ValueError("JIN10_WEB_URL cannot be empty")
        if not origin:
            raise ValueError("JIN10_WEB_ORIGIN cannot be empty")
        return cls(
            endpoint=endpoint,
            origin=origin,
            quote_frequency_ms=frequency_ms,
            quote_wait_timeout_seconds=wait_timeout,
            stale_after_seconds=stale_after,
            connect_timeout_seconds=connect_timeout,
        )
