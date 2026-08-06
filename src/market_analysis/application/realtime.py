from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from market_analysis.domain.errors import ProviderError
from market_analysis.domain.models import Instrument, QuoteSnapshot


class QuoteStreamState(StrEnum):
    CONNECTING = "connecting"
    LIVE = "live"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class QuoteStreamEvent:
    kind: str
    state: QuoteStreamState
    emitted_at: datetime
    quote: QuoteSnapshot | None = None
    error: str | None = None


FetchQuote = Callable[[Instrument, str], Awaitable[QuoteSnapshot]]
RecordQuote = Callable[[QuoteSnapshot], None]
PollInterval = Callable[[str], float]
IsPushSource = Callable[[str], bool]
_StreamKey = tuple[str, str]


@dataclass(slots=True)
class _Pump:
    source: str
    instrument: Instrument
    subscribers: set[asyncio.Queue[QuoteStreamEvent]]
    task: asyncio.Task[None] | None = None
    latest: QuoteStreamEvent | None = None
    latest_quote: QuoteSnapshot | None = None


class QuoteStreamCoordinator:
    """Shares one explicit-source quote pump across all UI subscribers."""

    def __init__(
        self,
        *,
        fetch_quote: FetchQuote,
        record_quote: RecordQuote,
        poll_interval: PollInterval,
        is_push_source: IsPushSource | None = None,
    ) -> None:
        self._fetch_quote = fetch_quote
        self._record_quote = record_quote
        self._poll_interval = poll_interval
        self._is_push_source = is_push_source or (lambda _: False)
        self._pumps: dict[_StreamKey, _Pump] = {}
        self._latest_recorded: dict[_StreamKey, QuoteSnapshot] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def subscribe(
        self,
        instrument: Instrument,
        *,
        source: str,
    ) -> AsyncIterator[asyncio.Queue[QuoteStreamEvent]]:
        key = (source, instrument.symbol)
        queue: asyncio.Queue[QuoteStreamEvent] = asyncio.Queue(maxsize=8)
        async with self._lock:
            pump = self._pumps.get(key)
            if pump is None:
                pump = _Pump(source=source, instrument=instrument, subscribers=set())
                self._pumps[key] = pump
            pump.subscribers.add(queue)
            if pump.latest is not None:
                queue.put_nowait(pump.latest)
            if pump.task is None or pump.task.done():
                pump.task = asyncio.create_task(
                    self._run(pump),
                    name=f"quote:{source}:{instrument.symbol}",
                )
        try:
            yield queue
        finally:
            async with self._lock:
                current = self._pumps.get(key)
                if current is not None:
                    current.subscribers.discard(queue)
                    if not current.subscribers:
                        if current.task is not None:
                            current.task.cancel()
                        self._pumps.pop(key, None)

    async def close(self) -> None:
        async with self._lock:
            tasks = [pump.task for pump in self._pumps.values() if pump.task is not None]
            self._pumps.clear()
            self._latest_recorded.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def publish_quote(self, quote: QuoteSnapshot) -> None:
        """Publishes an upstream push synchronously in the current event-loop turn."""
        key = (quote.source.provider, quote.instrument.symbol)
        pump = self._pumps.get(key)
        if pump is None:
            self._record_once(key, quote)
            return
        self._accept_quote(pump, quote)

    async def _run(self, pump: _Pump) -> None:
        self._broadcast(
            pump,
            QuoteStreamEvent(
                kind="status",
                state=QuoteStreamState.CONNECTING,
                emitted_at=datetime.now(UTC),
            ),
        )
        if self._is_push_source(pump.source):
            await self._run_push_source(pump)
            return
        await self._run_polled_source(pump)

    async def _run_push_source(self, pump: _Pump) -> None:
        """Seeds cached state once; later quotes arrive through publish_quote()."""
        await self._fetch_once(pump)
        await asyncio.Event().wait()

    async def _run_polled_source(self, pump: _Pump) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started_at = loop.time()
            await self._fetch_once(pump)
            interval = max(0.0, self._poll_interval(pump.source))
            elapsed = loop.time() - started_at
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _fetch_once(self, pump: _Pump) -> None:
        try:
            quote = await self._fetch_quote(pump.instrument, pump.source)
        except asyncio.CancelledError:
            raise
        except (ProviderError, ValueError) as error:
            event = QuoteStreamEvent(
                kind="status",
                state=QuoteStreamState.UNAVAILABLE,
                emitted_at=datetime.now(UTC),
                error=self._safe_error(error),
            )
            pump.latest = event
            self._broadcast(pump, event)
        else:
            self._accept_quote(pump, quote)

    def _accept_quote(self, pump: _Pump, quote: QuoteSnapshot) -> None:
        if quote == pump.latest_quote:
            return
        pump.latest_quote = quote
        self._record_once((pump.source, pump.instrument.symbol), quote)
        event = QuoteStreamEvent(
            kind="quote",
            state=QuoteStreamState.LIVE,
            emitted_at=datetime.now(UTC),
            quote=quote,
        )
        pump.latest = event
        self._broadcast(pump, event)

    def _record_once(self, key: _StreamKey, quote: QuoteSnapshot) -> None:
        if quote == self._latest_recorded.get(key):
            return
        self._latest_recorded[key] = quote
        self._record_quote(quote)

    @staticmethod
    def _broadcast(pump: _Pump, event: QuoteStreamEvent) -> None:
        for queue in tuple(pump.subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error).replace("\r", " ").replace("\n", " ").strip()
        return message[:240] or type(error).__name__
