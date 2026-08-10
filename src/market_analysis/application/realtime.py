from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from market_analysis.application.quotes import QuoteView
from market_analysis.domain.market_events import QuoteSample, RealtimeBar
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
    period_id: str = "1m"
    quote: QuoteView | None = None
    sample: QuoteSample | None = None
    bar: RealtimeBar | None = None
    error: str | None = None
    delivery_sequence: int | None = None
    gap_from_sequence: int | None = None
    gap_to_sequence: int | None = None


LoadQuote = Callable[[Instrument, str], Awaitable[QuoteView]]
_StreamKey = tuple[str, str, str]


@dataclass(slots=True)
class _Pump:
    source: str
    instrument: Instrument
    period: str
    subscribers: set[asyncio.Queue[QuoteStreamEvent]]
    latest: QuoteStreamEvent | None = None
    next_sequence: int = 0


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
        period: str = "1m",
    ) -> AsyncIterator[asyncio.Queue[QuoteStreamEvent]]:
        if not period.strip():
            raise ValueError("period cannot be empty")
        key = (source, instrument.symbol, period)
        # The durable event log owns lossless replay. A browser connection is a live
        # projection and must remain bounded; every Bar is a complete replacement, so
        # dropping the oldest stalled delivery cannot corrupt the next upsert.
        queue: asyncio.Queue[QuoteStreamEvent] = asyncio.Queue(maxsize=512)
        queue.put_nowait(
            QuoteStreamEvent(
                kind="status",
                state=QuoteStreamState.CONNECTING,
                emitted_at=datetime.now(UTC),
                period_id=period,
            )
        )
        async with self._lock:
            pump = self._pumps.get(key)
            if pump is None:
                pump = _Pump(
                    source=source,
                    instrument=instrument,
                    period=period,
                    subscribers=set(),
                )
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

    def active_periods(self, instrument: Instrument, *, source: str) -> frozenset[str]:
        """Returns the resolutions currently consumed by browser connections."""

        return frozenset(
            period
            for candidate_source, symbol, period in self._pumps
            if candidate_source == source and symbol == instrument.symbol
        )

    def publish(self, view: QuoteView) -> None:
        for pump in self._matching_pumps(view.source_id, view.quote.instrument):
            event = QuoteStreamEvent(
                kind="quote",
                state=QuoteStreamState.LIVE,
                emitted_at=datetime.now(UTC),
                period_id=pump.period,
                quote=view,
            )
            self._broadcast(pump, event)

    def publish_sample(self, sample: QuoteSample) -> None:
        for pump in self._matching_pumps(sample.source_id, sample.instrument):
            event = QuoteStreamEvent(
                kind="sample",
                state=QuoteStreamState.LIVE,
                emitted_at=datetime.now(UTC),
                period_id=pump.period,
                sample=sample,
            )
            self._broadcast(pump, event)

    def publish_bar_update(
        self,
        bar: RealtimeBar,
        *,
        period_id: str | None = None,
    ) -> None:
        period = period_id or self._period_from_bar(bar)
        pump = self._pumps.get((bar.source.provider, bar.instrument.symbol, period))
        if pump is None:
            return
        self._broadcast(
            pump,
            QuoteStreamEvent(
                kind="bar",
                state=QuoteStreamState.LIVE,
                emitted_at=datetime.now(UTC),
                period_id=period,
                bar=bar,
            ),
        )

    def publish_unavailable(self, instrument: Instrument, source: str, error: Exception) -> None:
        for pump in self._matching_pumps(source, instrument):
            event = QuoteStreamEvent(
                kind="status",
                state=QuoteStreamState.UNAVAILABLE,
                emitted_at=datetime.now(UTC),
                period_id=pump.period,
                error=self._safe_error(error),
            )
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
                period_id=pump.period,
                error=self._safe_error(error),
            )
        else:
            event = QuoteStreamEvent(
                kind="quote",
                state=QuoteStreamState.LIVE,
                emitted_at=datetime.now(UTC),
                period_id=pump.period,
                quote=view,
            )
        if pump.latest is None:
            pump.latest = event
            if not queue.full():
                queue.put_nowait(event)

    @staticmethod
    def _broadcast(pump: _Pump, event: QuoteStreamEvent) -> QuoteStreamEvent:
        pump.next_sequence += 1
        delivered = replace(event, delivery_sequence=pump.next_sequence)
        if delivered.kind in {"quote", "status"}:
            pump.latest = delivered
        for queue in tuple(pump.subscribers):
            if queue.full():
                dropped: list[QuoteStreamEvent] = []
                while not queue.empty():
                    dropped.append(queue.get_nowait())
                sequences = [
                    item.delivery_sequence
                    for item in dropped
                    if item.delivery_sequence is not None
                ]
                fallback = max(1, delivered.delivery_sequence - 1)
                queue.put_nowait(
                    QuoteStreamEvent(
                        kind="gap",
                        state=delivered.state,
                        emitted_at=datetime.now(UTC),
                        period_id=delivered.period_id,
                        gap_from_sequence=min(sequences, default=fallback),
                        gap_to_sequence=max(sequences, default=fallback),
                    )
                )
            queue.put_nowait(delivered)
        return delivered

    def _matching_pumps(self, source: str, instrument: Instrument) -> tuple[_Pump, ...]:
        return tuple(
            pump
            for (candidate_source, candidate_symbol, _), pump in self._pumps.items()
            if candidate_source == source and candidate_symbol == instrument.symbol
        )

    @staticmethod
    def _period_from_bar(bar: RealtimeBar) -> str:
        raw = bar.source.raw_payload
        raw_period = raw.get("period_id") if raw else None
        if isinstance(raw_period, str) and raw_period.strip():
            return raw_period
        interval_seconds = int(bar.interval.total_seconds())
        if interval_seconds == 1:
            return "1s"
        if interval_seconds == 60:
            return "1m"
        raise ValueError("period_id is required for a non-base Bar interval")

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error).replace("\r", " ").replace("\n", " ").strip()
        return message[:240] or type(error).__name__
