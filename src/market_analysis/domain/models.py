from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Generic, TypeVar


class AssetClass(StrEnum):
    SPOT = "spot"
    FUTURE = "future"
    FOREX = "forex"
    EQUITY = "equity"
    INDEX = "index"
    ENERGY = "energy"
    METAL = "metal"
    CRYPTO = "crypto"
    UNKNOWN = "unknown"


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _require_finite(value: Decimal, field: str) -> None:
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    asset_class: AssetClass
    base: str | None = None
    quote: str | None = None
    venue: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol cannot be empty")


@dataclass(frozen=True, slots=True)
class InstrumentCatalogEntry:
    provider: str
    provider_code: str
    name: str
    instrument: Instrument | None = None


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    provider: str
    provider_symbol: str
    observed_at: datetime
    received_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.received_at, "received_at")


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    instrument: Instrument
    last: Decimal
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    volume: Decimal | None
    change: Decimal | None
    change_percent: Decimal | None
    source: SourceMetadata

    def __post_init__(self) -> None:
        _require_finite(self.last, "last")
        for field in ("open", "high", "low"):
            value = getattr(self, field)
            if value is not None:
                _require_finite(value, field)
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError("low cannot be greater than high")
        if (
            self.low is not None
            and self.high is not None
            and not self.low <= self.last <= self.high
        ):
            raise ValueError("last must be within low and high")
        if self.volume is not None and self.volume < 0:
            raise ValueError("volume cannot be negative")


@dataclass(frozen=True, slots=True)
class Candle:
    instrument: Instrument
    interval: timedelta
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    source: SourceMetadata

    def __post_init__(self) -> None:
        _require_aware(self.open_time, "open_time")
        if self.interval <= timedelta(0):
            raise ValueError("interval must be positive")
        for field in ("open", "high", "low", "close"):
            _require_finite(getattr(self, field), field)
        if self.low > self.high:
            raise ValueError("low cannot be greater than high")
        if not self.low <= self.open <= self.high:
            raise ValueError("open must be within low and high")
        if not self.low <= self.close <= self.high:
            raise ValueError("close must be within low and high")
        if self.volume is not None and self.volume < 0:
            raise ValueError("volume cannot be negative")


@dataclass(frozen=True, slots=True)
class FlashItem:
    title: str
    content: str
    published_at: datetime
    url: str
    source_provider: str

    def __post_init__(self) -> None:
        _require_aware(self.published_at, "published_at")


@dataclass(frozen=True, slots=True)
class NewsBrief:
    article_id: str
    title: str
    introduction: str
    published_at: datetime
    url: str
    source_provider: str

    def __post_init__(self) -> None:
        _require_aware(self.published_at, "published_at")


@dataclass(frozen=True, slots=True)
class NewsArticle:
    article_id: str
    title: str
    introduction: str
    content: str
    published_at: datetime
    url: str
    source_provider: str

    def __post_init__(self) -> None:
        _require_aware(self.published_at, "published_at")


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    published_at: datetime
    importance: int
    title: str
    previous: str | None
    consensus: str | None
    actual: str | None
    revised: str | None
    impact_text: str
    source_provider: str

    def __post_init__(self) -> None:
        _require_aware(self.published_at, "published_at")
        if self.importance < 0:
            raise ValueError("importance cannot be negative")


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class FeedPage(Generic[T]):
    items: tuple[T, ...]
    next_cursor: str
    has_more: bool
