from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CboeVolatilitySettings:
    quote_base_url: str = "https://cdn.cboe.com/api/global/delayed_quotes/quotes"
    history_base_url: str = "https://cdn.cboe.com/api/global/us_indices/daily_prices"
    timeout_seconds: float = 10.0
    quote_cache_ttl_seconds: float = 30.0
    history_cache_ttl_seconds: float = 21_600.0

    def __post_init__(self) -> None:
        if not self.quote_base_url.strip() or not self.history_base_url.strip():
            raise ValueError("Cboe endpoint URLs cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("Cboe timeout must be positive")
        if self.quote_cache_ttl_seconds < 0 or self.history_cache_ttl_seconds < 0:
            raise ValueError("Cboe cache TTL cannot be negative")

    def quote_url(self, index_code: str) -> str:
        return f"{self.quote_base_url.rstrip('/')}/_{index_code.upper()}.json"

    def history_url(self, index_code: str) -> str:
        return f"{self.history_base_url.rstrip('/')}/{index_code.upper()}_History.csv"
