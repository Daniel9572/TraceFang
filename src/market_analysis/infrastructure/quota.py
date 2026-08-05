from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from market_analysis.domain.errors import ProviderRateLimitError


class DailyToolBudget:
    """Process-local guard; upstream remains authoritative across processes."""

    def __init__(
        self,
        *,
        provider: str,
        daily_limit: int,
        reserve: int = 0,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        if daily_limit <= 0:
            raise ValueError("daily_limit must be positive")
        if not 0 <= reserve < daily_limit:
            raise ValueError("reserve must be between zero and daily_limit")
        self.provider = provider
        self.daily_limit = daily_limit
        self.reserve = reserve
        self._timezone = ZoneInfo(timezone)
        self._date = datetime.now(self._timezone).date()
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def acquire(self, tool_name: str) -> None:
        async with self._lock:
            today = datetime.now(self._timezone).date()
            if today != self._date:
                self._date = today
                self._counts.clear()
            usable_limit = self.daily_limit - self.reserve
            if self._counts[tool_name] >= usable_limit:
                raise ProviderRateLimitError(
                    f"{self.provider}.{tool_name} local daily budget exhausted"
                )
            self._counts[tool_name] += 1

    def used(self, tool_name: str) -> int:
        return self._counts[tool_name]
