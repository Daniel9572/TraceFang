from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from market_analysis.domain.models import Candle, Instrument, QuoteSnapshot


@dataclass(frozen=True, slots=True)
class PersistenceHealth:
    state: str
    detail: str | None
    queue_depth: int
    last_write_at: datetime | None


class MarketDataStore(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def save_quote(self, quote: QuoteSnapshot) -> None: ...

    async def save_candles(self, candles: Sequence[Candle]) -> None: ...

    async def load_candles(
        self,
        instrument: Instrument,
        *,
        source_id: str,
        interval: timedelta = timedelta(minutes=1),
        start: datetime | None = None,
        count: int = 100,
    ) -> tuple[Candle, ...]: ...


class MarketDataWriter(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def submit_quote(self, quote: QuoteSnapshot) -> bool: ...

    def submit_candles(self, candles: Sequence[Candle]) -> bool: ...

    def health(self) -> PersistenceHealth: ...
