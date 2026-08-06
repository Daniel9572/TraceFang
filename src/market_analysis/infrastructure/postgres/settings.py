from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    dsn: str
    min_pool_size: int = 1
    max_pool_size: int = 5
    command_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> PostgresSettings | None:
        dsn = os.environ.get("MARKET_ANALYSIS_DATABASE_URL", "").strip()
        if not dsn:
            return None
        min_pool_size = int(os.environ.get("MARKET_ANALYSIS_DB_MIN_POOL_SIZE", "1"))
        max_pool_size = int(os.environ.get("MARKET_ANALYSIS_DB_MAX_POOL_SIZE", "5"))
        timeout = float(os.environ.get("MARKET_ANALYSIS_DB_COMMAND_TIMEOUT_SECONDS", "10"))
        if min_pool_size < 1:
            raise ValueError("MARKET_ANALYSIS_DB_MIN_POOL_SIZE must be at least 1")
        if max_pool_size < min_pool_size:
            raise ValueError("MARKET_ANALYSIS_DB_MAX_POOL_SIZE must be >= min pool size")
        if timeout <= 0:
            raise ValueError("MARKET_ANALYSIS_DB_COMMAND_TIMEOUT_SECONDS must be positive")
        return cls(
            dsn=dsn,
            min_pool_size=min_pool_size,
            max_pool_size=max_pool_size,
            command_timeout_seconds=timeout,
        )
