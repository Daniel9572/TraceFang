from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from tracefang.infrastructure.providers.jin10_local.session import (
    Jin10SessionCredentials,
    Jin10SessionResolver,
)

DEFAULT_JIN10_LOCAL_URL = "wss://app-quote-ws.jin10.com/"
DEFAULT_JIN10_KLINE_FILE_URL = "https://jiaoyixia-market.jin10.com"


@dataclass(frozen=True, slots=True)
class Jin10LocalSettings:
    session_resolver: Jin10SessionResolver = field(repr=False)
    endpoint: str = DEFAULT_JIN10_LOCAL_URL
    kline_file_endpoint: str = DEFAULT_JIN10_KLINE_FILE_URL
    quote_frequency_ms: int = 1000
    kline_frequency_ms: int = 3000
    quote_wait_timeout_seconds: float = 10.0
    kline_wait_timeout_seconds: float = 12.0
    kline_download_timeout_seconds: float = 20.0
    stale_after_seconds: float = 12.0
    connect_timeout_seconds: float = 10.0
    heartbeat_seconds: float = 10.0
    vip_type: int = 3

    @classmethod
    def for_credentials(
        cls,
        *,
        session_token: str,
        **kwargs: object,
    ) -> Jin10LocalSettings:
        return cls(
            session_resolver=Jin10SessionResolver.fixed(
                session_token=session_token,
            ),
            **kwargs,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Jin10LocalSettings:
        values = os.environ if env is None else env
        resolver = Jin10SessionResolver(values)
        # Session availability is runtime state, not startup configuration.  This
        # keeps the provider recoverable when the desktop client logs in later.
        resolver.validate_configuration()

        frequency_ms = int(values.get("JIN10_LOCAL_QUOTE_FREQUENCY_MS", "1000"))
        kline_frequency_ms = int(values.get("JIN10_LOCAL_KLINE_FREQUENCY_MS", "3000"))
        wait_timeout = float(values.get("JIN10_LOCAL_QUOTE_WAIT_TIMEOUT_SECONDS", "10"))
        kline_wait_timeout = float(values.get("JIN10_LOCAL_KLINE_WAIT_TIMEOUT_SECONDS", "12"))
        kline_download_timeout = float(
            values.get("JIN10_LOCAL_KLINE_DOWNLOAD_TIMEOUT_SECONDS", "20")
        )
        stale_after = float(values.get("JIN10_LOCAL_STALE_AFTER_SECONDS", "12"))
        connect_timeout = float(values.get("JIN10_LOCAL_CONNECT_TIMEOUT_SECONDS", "10"))
        heartbeat = float(values.get("JIN10_LOCAL_HEARTBEAT_SECONDS", "10"))
        vip_type = int(values.get("JIN10_LOCAL_VIP_TYPE", "3"))
        if not 250 <= frequency_ms <= 60_000:
            raise ValueError("JIN10_LOCAL_QUOTE_FREQUENCY_MS must be between 250 and 60000")
        if not 250 <= kline_frequency_ms <= 60_000:
            raise ValueError("JIN10_LOCAL_KLINE_FREQUENCY_MS must be between 250 and 60000")
        if (
            min(
                wait_timeout,
                kline_wait_timeout,
                kline_download_timeout,
                stale_after,
                connect_timeout,
                heartbeat,
            )
            <= 0
        ):
            raise ValueError("Jin10 local timeout values must be positive")
        if vip_type not in {0, 1, 3}:
            raise ValueError("JIN10_LOCAL_VIP_TYPE must be 0, 1, or 3")
        return cls(
            session_resolver=resolver,
            endpoint=values.get("JIN10_LOCAL_URL", DEFAULT_JIN10_LOCAL_URL).strip(),
            kline_file_endpoint=values.get(
                "JIN10_LOCAL_KLINE_FILE_URL",
                DEFAULT_JIN10_KLINE_FILE_URL,
            )
            .strip()
            .rstrip("/"),
            quote_frequency_ms=frequency_ms,
            kline_frequency_ms=kline_frequency_ms,
            quote_wait_timeout_seconds=wait_timeout,
            kline_wait_timeout_seconds=kline_wait_timeout,
            kline_download_timeout_seconds=kline_download_timeout,
            stale_after_seconds=stale_after,
            connect_timeout_seconds=connect_timeout,
            heartbeat_seconds=heartbeat,
            vip_type=vip_type,
        )

    def credentials(self, *, refresh: bool = False) -> Jin10SessionCredentials:
        return self.session_resolver.resolve(refresh=refresh)

    def redact(self, message: str) -> str:
        return self.session_resolver.redact(message)


__all__ = [
    "DEFAULT_JIN10_KLINE_FILE_URL",
    "DEFAULT_JIN10_LOCAL_URL",
    "Jin10LocalSettings",
]
