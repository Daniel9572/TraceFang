from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tracefang.domain.errors import ProviderRateLimitError


@dataclass(frozen=True, slots=True)
class ToolBudgetSnapshot:
    provider: str
    tool_name: str
    used: int
    limit: int
    reserve: int
    available: int
    usage_percent: float
    period: str
    resets_at: datetime
    scope: str = "application_process"


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

    def _rollover(self, now: datetime) -> None:
        today = now.date()
        if today != self._date:
            self._date = today
            self._counts.clear()

    async def acquire(self, tool_name: str) -> None:
        async with self._lock:
            now = datetime.now(self._timezone)
            self._rollover(now)
            usable_limit = self.daily_limit - self.reserve
            if self._counts[tool_name] >= usable_limit:
                raise ProviderRateLimitError(
                    f"{self.provider}.{tool_name} local daily budget exhausted"
                )
            self._counts[tool_name] += 1

    async def snapshots(self, tool_names: Iterable[str]) -> tuple[ToolBudgetSnapshot, ...]:
        async with self._lock:
            now = datetime.now(self._timezone)
            self._rollover(now)
            reset_date = now.date() + timedelta(days=1)
            resets_at = datetime.combine(reset_date, datetime.min.time(), self._timezone)
            rows: list[ToolBudgetSnapshot] = []
            for tool_name in tool_names:
                used = self._counts[tool_name]
                rows.append(
                    ToolBudgetSnapshot(
                        provider=self.provider,
                        tool_name=tool_name,
                        used=used,
                        limit=self.daily_limit,
                        reserve=self.reserve,
                        available=max(0, self.daily_limit - self.reserve - used),
                        usage_percent=min(100.0, used / self.daily_limit * 100),
                        period="daily",
                        resets_at=resets_at,
                    )
                )
            return tuple(rows)

    def used(self, tool_name: str) -> int:
        return self._counts[tool_name]
