from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShfePositioningSettings:
    base_url: str = "https://www.shfe.com.cn/data/tradedata/future/delaymarket"
    timeout_seconds: float = 10.0
    cache_ttl_seconds: float = 60.0
    declared_delay_minutes: int = 30

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ValueError("SHFE delayed market base URL cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("SHFE positioning timeout must be positive")
        if self.cache_ttl_seconds < 0:
            raise ValueError("SHFE positioning cache TTL cannot be negative")
        if self.declared_delay_minutes < 0:
            raise ValueError("SHFE declared delay cannot be negative")

    def data_url(self, product_code: str) -> str:
        return f"{self.base_url.rstrip('/')}/delaymarket_{product_code.lower()}.dat"
