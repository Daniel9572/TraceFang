from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from market_analysis.application.period_bars import PeriodBarService
from market_analysis.application.provider_frames import ProviderFrame
from market_analysis.application.realtime_bars import RealtimeBarContract, RealtimeBarService
from market_analysis.domain.errors import ProviderError
from market_analysis.domain.market_events import RealtimeBar
from market_analysis.domain.models import Candle, Instrument, QuoteSnapshot

DecodedMarketValue = QuoteSnapshot | Candle
FrameDecoder = Callable[[ProviderFrame], Awaitable[Sequence[DecodedMarketValue]]]


@dataclass(frozen=True, slots=True)
class ReplayStreamEvent:
    """One ordered projection emitted from an immutable provider frame."""

    kind: str
    stream_sequence: int
    frame_received_at: str
    frame_channel: str
    period_id: str
    source_id: str
    quote: QuoteSnapshot | None = None
    bar: RealtimeBar | None = None
    error: str | None = None


class MarketReplayProjector:
    """Isolated replay reducer that never mutates live state or persistence.

    The durable provider frame remains the evidence. This object uses the same
    provider decoder, RealtimeBarService and PeriodBarService as the live path to
    build a disposable chart projection for one browser session.
    """

    def __init__(
        self,
        *,
        contracts: tuple[RealtimeBarContract, ...],
        decode_frame: FrameDecoder,
        instrument: Instrument,
        source_id: str,
        period_id: str,
        schedule: Mapping[str, Any] | None,
    ) -> None:
        self._decode_frame = decode_frame
        self._instrument = instrument
        self._source_id = source_id
        self._period_id = "1s" if period_id == "timeline" else period_id
        self._schedule = schedule
        self._bars = RealtimeBarService(None, contracts=contracts)
        self._period_bars = PeriodBarService(self._bars)
        self._warmup_open_time: datetime | None = None

    async def close(self) -> None:
        await self._bars.close()

    async def accept_frame(
        self,
        stream_sequence: int,
        frame: ProviderFrame,
    ) -> tuple[ReplayStreamEvent, ...]:
        base = {
            "stream_sequence": stream_sequence,
            "frame_received_at": frame.received_at.isoformat(),
            "frame_channel": frame.channel,
            "period_id": self._period_id,
            "source_id": self._source_id,
        }
        events: list[ReplayStreamEvent] = [ReplayStreamEvent(kind="frame", **base)]
        try:
            decoded = await self._decode_frame(frame)
        except (ProviderError, ValueError, TypeError) as error:
            events.append(
                ReplayStreamEvent(
                    kind="decode_error",
                    error=self._safe_error(error),
                    **base,
                )
            )
            return tuple(events)

        for value in decoded:
            if value.instrument != self._instrument:
                continue
            if isinstance(value, QuoteSnapshot):
                normalized = self._bars.normalize_quote(value)
                if normalized is None or normalized.source_id != self._source_id:
                    continue
                events.extend(self._bar_events(self._bars.apply(normalized), base))
                events.append(ReplayStreamEvent(kind="quote", quote=value, **base))
                continue
            normalized = self._bars.normalize_bar(value)
            if normalized is None or normalized.source_id != self._source_id:
                continue
            events.extend(self._bar_events(self._bars.apply(normalized), base))
        return tuple(events)

    def _bar_events(
        self,
        transitions: Sequence[RealtimeBar],
        base: dict[str, object],
    ) -> tuple[ReplayStreamEvent, ...]:
        events: list[ReplayStreamEvent] = []
        for bar in transitions:
            interval_seconds = int(bar.interval.total_seconds())
            if self._period_id == "1s" and interval_seconds == 1:
                if self._bar_is_complete_after_warmup(bar):
                    events.append(ReplayStreamEvent(kind="bar", bar=bar, **base))
                continue
            if self._period_id == "1m" and interval_seconds == 60:
                if self._bar_is_complete_after_warmup(bar):
                    events.append(ReplayStreamEvent(kind="bar", bar=bar, **base))
                continue
            if interval_seconds != 60 or self._period_id in {"1s", "1m"}:
                continue
            for period_id, projected in self._period_bars.accept_live(
                bar,
                schedule=self._schedule,
                period_ids=(self._period_id,),
            ):
                if period_id == self._period_id and self._bar_is_complete_after_warmup(projected):
                    events.append(ReplayStreamEvent(kind="bar", bar=projected, **base))
        return tuple(events)

    def _bar_is_complete_after_warmup(self, bar: RealtimeBar) -> bool:
        """Never render the retained stream's potentially truncated first bucket."""

        if self._warmup_open_time is None:
            self._warmup_open_time = bar.open_time
            return False
        return bar.open_time > self._warmup_open_time

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error).replace("\r", " ").replace("\n", " ").strip()
        return message[:240] or type(error).__name__
