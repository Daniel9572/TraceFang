from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_SHFE_BASE_URL = "https://www.shfe.com.cn"


def _as_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("SHFE_GOLD_OPTIONS_ENABLED must be a boolean")


@dataclass(frozen=True, slots=True)
class ShfeGoldOptionsSettings:
    enabled: bool = True
    base_url: str = DEFAULT_SHFE_BASE_URL
    request_timeout_seconds: float = 12.0
    snapshot_cache_seconds: float = 10.0

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> ShfeGoldOptionsSettings:
        values = os.environ if env is None else env
        enabled = _as_bool(values.get("SHFE_GOLD_OPTIONS_ENABLED", "true"))
        base_url = values.get("SHFE_GOLD_OPTIONS_BASE_URL", DEFAULT_SHFE_BASE_URL).strip()
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != "www.shfe.com.cn":
            raise ValueError(
                "SHFE_GOLD_OPTIONS_BASE_URL must use the official https://www.shfe.com.cn host"
            )
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("SHFE_GOLD_OPTIONS_BASE_URL cannot contain a path, query, or fragment")
        timeout = float(values.get("SHFE_GOLD_OPTIONS_REQUEST_TIMEOUT_SECONDS", "12"))
        cache_seconds = float(values.get("SHFE_GOLD_OPTIONS_CACHE_SECONDS", "10"))
        if timeout <= 0 or cache_seconds <= 0:
            raise ValueError("SHFE gold option timeout and cache values must be positive")
        return cls(
            enabled=enabled,
            base_url=base_url.rstrip("/"),
            request_timeout_seconds=timeout,
            snapshot_cache_seconds=cache_seconds,
        )
