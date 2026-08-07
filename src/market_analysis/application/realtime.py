from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from market_analysis.application.quotes import QuoteView
from market_analysis.domain.models import Instrument


class QuoteStreamState(StrEnum):
    CONNECTING = "connecting"
    LIVE = "live"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class QuoteStreamEvent:
    kind: str
    state: QuoteStreamState
    emitted_at: datetime
    quote: QuoteView | None = None
    error: str | None = None


LoadQuote = Callable[[Instrument, str], Awaitable[QuoteView]]
_StreamKey = tuple[str, str]


@dataclass(slots=True)
class _Pump:
    source: str
    instrument: Instrument
    subscribers: set[asyncio.Queue[QuoteStreamEvent]]
    latest: QuoteStreamEvent | None = None


class QuoteStreamCoordinator:
    """Passive fan-out for locally cached quotes; it never calls an upstream feed."""

    def __init__(self, *, load_quote: LoadQuote) -> None:
        self._load_quote = load_quote
        self._pumps: dict[_StreamKey, _Pump] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def subscribe(
        self,
        instrument: Instrument,
        *,
        source: str,
    ) -> AsyncIterator[asyncio.Queue[QuoteStreamEvent]]:
        key = (source, instrument.symbol)
        # A bounded queue would silently discard intermediate price changes when a
        # browser or socket stalls for only a moment. The live path preserves every
        # accepted frame so application-level buffering never manufactures gaps.
        queue: asyncio.Queue[QuoteStreamEvent] = asyncio.Queue()
        queue.put_nowait(
            QuoteStreamEvent(
                kind="status",
                state=QuoteStreamState.CONNECTING,
                emitted_at=datetime.now(UTC),
            )
        )
        async with self._lock:
            pump = self._pumps.get(key)
            if pump is None:
                pump = _Pump(source=source, instrument=instrument, subscribers=set())
                self._pumps[key] = pump
            pump.subscribers.add(queue)
            latest = pump.latest
        if latest is not None:
            queue.put_nowait(latest)
        else:
            await self._seed_from_local_cache(pump, queue)
        try:
            yield queue
        finally:
            async with self._lock:
                current = self._pumps.get(key)
                if current is not None:
                    current.subscribers.discard(queue)
                    if not current.subscribers:
                        self._pumps.pop(key, None)

    async def close(self) -> None:
        async with self._lock:
            self._pumps.clear()

    def publish(self, view: QuoteView) -> None:
        key = (view.source_id, view.quote.instrument.symbol)
        pump = self._pumps.get(key)
        if pump is None:
            return
        event = QuoteStreamEvent(
            kind="quote",
            state=QuoteStreamState.LIVE,
            emitted_at=datetime.now(UTC),
            quote=view,
        )
        pump.latest = event
        self._broadcast(pump, event)

    def publish_unavailable(self, instrument: Instrument, source: str, error: Exception) -> None:
        pump = self._pumps.get((source, instrument.symbol))
        if pump is None:
            return
        event = QuoteStreamEvent(
            kind="status",
            state=QuoteStreamState.UNAVAILABLE,
            emitted_at=datetime.now(UTC),
            error=self._safe_error(error),
        )
        pump.latest = event
        self._broadcast(pump, event)

    async def _seed_from_local_cache(
        self,
        pump: _Pump,
        queue: asyncio.Queue[QuoteStreamEvent],
    ) -> None:
        try:
            view = await self._load_quote(pump.instrument, pump.source)
        except Exception as error:
            event = QuoteStreamEvent(
                kind="status",
                state=QuoteStreamState.UNAVAILABLE,
                emitted_at=datetime.now(UTC),
                error=self._safe_error(error),
            )
        else:
            event = QuoteStreamEvent(
                kind="quote",
                state=QuoteStreamState.LIVE,
                emitted_at=datetime.now(UTC),
                quote=view,
            )
        if pump.latest is None:
            pump.latest = event
            if not queue.full():
                queue.put_nowait(event)

    @staticmethod
    def _broadcast(pump: _Pump, event: QuoteStreamEvent) -> None:
        for queue in tuple(pump.subscribers):
            queue.put_nowait(event)

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error).replace("\r", " ").replace("\n", " ").strip()
        return message[:240] or type(error).__name__
