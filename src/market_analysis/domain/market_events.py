from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from market_analysis.domain.models import Candle, Instrument, QuoteSnapshot


class BarState(StrEnum):
    """Lifecycle state of one realtime-source Bar projection."""

    PROVISIONAL_QUOTE = "provisional_quote"
    PROVISIONAL_AUTHORITATIVE = "provisional_authoritative"
    FINAL = "final"


class BarFinalityPolicy(StrEnum):
    """How a source proves that an authoritative Bar has closed."""

    EXPLICIT = "explicit"
    NEXT_AUTHORITATIVE_BAR = "next_authoritative_bar"


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    """One normalized quote routed into a complete realtime source."""

    source_id: str
    channel_id: str
    quote: QuoteSnapshot
    sequence: int | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("source_id cannot be empty")
        if not self.channel_id.strip():
            raise ValueError("channel_id cannot be empty")
        if self.quote.source.provider != self.channel_id:
            raise ValueError("quote event channel must match its raw evidence provider")
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("sequence cannot be negative")


@dataclass(frozen=True, slots=True)
class BarEvent:
    """One normalized authoritative Bar routed into a complete realtime source."""

    source_id: str
    channel_id: str
    candle: Candle
    state: BarState = BarState.PROVISIONAL_AUTHORITATIVE
    sequence: int | None = None
    finalized_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("source_id cannot be empty")
        if not self.channel_id.strip():
            raise ValueError("channel_id cannot be empty")
        if self.candle.source.provider != self.channel_id:
            raise ValueError("Bar event channel must match its raw evidence provider")
        if self.state is BarState.PROVISIONAL_QUOTE:
            raise ValueError("an authoritative Bar event cannot have quote-only state")
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("sequence cannot be negative")
        if self.finalized_at is not None and (
            self.finalized_at.tzinfo is None or self.finalized_at.utcoffset() is None
        ):
            raise ValueError("finalized_at must be timezone-aware")
        if self.state is not BarState.FINAL and self.finalized_at is not None:
            raise ValueError("only a final Bar event can have finalized_at")


MarketEvent = QuoteEvent | BarEvent


@dataclass(frozen=True, slots=True)
class QuoteSample:
    """One lossless timeline sample; source and raw evidence channel stay distinct."""

    source_id: str
    channel_id: str
    event_id: str
    instrument: Instrument
    provider_symbol: str
    observed_at: datetime
    received_at: datetime
    value: Decimal
    storage_id: int | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.channel_id.strip():
            raise ValueError("sample source and channel cannot be empty")
        if not self.event_id.strip() or not self.provider_symbol.strip():
            raise ValueError("sample identity cannot be empty")
        for field in ("observed_at", "received_at"):
            value = getattr(self, field)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field} must be timezone-aware")
        if not self.value.is_finite():
            raise ValueError("sample value must be finite")
        if self.storage_id is not None and self.storage_id < 1:
            raise ValueError("storage_id must be positive")


@dataclass(frozen=True, slots=True)
class RealtimeBar(Candle):
    """Source-bound Bar projection exposed to storage, API, and chart consumers."""

    evidence_channel_id: str
    state: BarState
    revision: int = 1
    finalized_at: datetime | None = None

    def __post_init__(self) -> None:
        Candle.__post_init__(self)
        if not self.evidence_channel_id.strip():
            raise ValueError("evidence_channel_id cannot be empty")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if self.finalized_at is not None and (
            self.finalized_at.tzinfo is None or self.finalized_at.utcoffset() is None
        ):
            raise ValueError("finalized_at must be timezone-aware")
        if self.state is BarState.FINAL and self.finalized_at is None:
            raise ValueError("a final Bar requires finalized_at")
        if self.state is not BarState.FINAL and self.finalized_at is not None:
            raise ValueError("only a final Bar can have finalized_at")
