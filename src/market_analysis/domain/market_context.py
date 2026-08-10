from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _require_finite(value: Decimal | None, field: str) -> None:
    if value is not None and not value.is_finite():
        raise ValueError(f"{field} must be finite")


class PositionCountingMethod(StrEnum):
    SINGLE_SIDE = "single_side"


class DirectionalInference(StrEnum):
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MarketContextSource:
    provider_id: str
    dataset_id: str
    source_url: str
    observed_at: datetime
    received_at: datetime
    published_at: datetime | None
    delayed: bool
    declared_delay: timedelta | None

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.dataset_id.strip():
            raise ValueError("market context source identity cannot be empty")
        if not self.source_url.strip():
            raise ValueError("market context source URL cannot be empty")
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.received_at, "received_at")
        if self.published_at is not None:
            _require_aware(self.published_at, "published_at")
        if self.declared_delay is not None and self.declared_delay < timedelta(0):
            raise ValueError("declared_delay cannot be negative")


@dataclass(frozen=True, slots=True)
class EndOfDayMarketContextSource:
    provider_id: str
    dataset_id: str
    source_url: str
    as_of: date
    received_at: datetime
    frequency: str = "daily_eod"

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.dataset_id.strip():
            raise ValueError("end-of-day context source identity cannot be empty")
        if not self.source_url.strip():
            raise ValueError("end-of-day context source URL cannot be empty")
        _require_aware(self.received_at, "received_at")
        if self.frequency != "daily_eod":
            raise ValueError("volatility history must retain its daily EOD frequency")


@dataclass(frozen=True, slots=True)
class VolatilityIndexEodContext:
    index_code: str
    underlying: str
    value: Decimal
    trailing_percentile_252: Decimal | None
    history_sample_size: int
    history_start: date | None
    history_end: date | None
    source: EndOfDayMarketContextSource
    expected_horizon_days: int = 30
    directional: bool = False

    def __post_init__(self) -> None:
        if not self.index_code.strip() or not self.underlying.strip():
            raise ValueError("volatility index identity cannot be empty")
        _require_finite(self.value, "value")
        _require_finite(self.trailing_percentile_252, "trailing_percentile_252")
        if self.value <= 0:
            raise ValueError("volatility index value must be positive")
        if self.trailing_percentile_252 is not None and not (
            Decimal(0) <= self.trailing_percentile_252 <= Decimal(100)
        ):
            raise ValueError("trailing percentile must be between 0 and 100")
        if not 0 <= self.history_sample_size <= 252:
            raise ValueError("history_sample_size must be between 0 and 252")
        if self.history_sample_size == 0:
            if any(
                value is not None
                for value in (
                    self.trailing_percentile_252,
                    self.history_start,
                    self.history_end,
                )
            ):
                raise ValueError("empty volatility history cannot expose percentile bounds")
        elif any(value is None for value in (self.history_start, self.history_end)):
            raise ValueError("non-empty volatility history requires date bounds")
        elif (
            self.history_start is not None
            and self.history_end is not None
            and self.history_start > self.history_end
        ):
            raise ValueError("volatility history bounds are reversed")
        if self.history_end != self.source.as_of:
            raise ValueError("volatility EOD source date must match the latest history value")
        if self.expected_horizon_days <= 0:
            raise ValueError("expected_horizon_days must be positive")
        if self.directional:
            raise ValueError("volatility magnitude indices are not directional signals")


@dataclass(frozen=True, slots=True)
class VolatilityIndexContext:
    index_code: str
    underlying: str
    value: Decimal
    change: Decimal | None
    change_percent: Decimal | None
    session_open: Decimal | None
    session_high: Decimal | None
    session_low: Decimal | None
    previous_close: Decimal | None
    trailing_percentile_252: Decimal | None
    history_sample_size: int
    history_start: date | None
    history_end: date | None
    source: MarketContextSource
    expected_horizon_days: int = 30
    directional: bool = False

    def __post_init__(self) -> None:
        if not self.index_code.strip() or not self.underlying.strip():
            raise ValueError("volatility index identity cannot be empty")
        for field in (
            "value",
            "change",
            "change_percent",
            "session_open",
            "session_high",
            "session_low",
            "previous_close",
            "trailing_percentile_252",
        ):
            _require_finite(getattr(self, field), field)
        if self.value <= 0:
            raise ValueError("volatility index value must be positive")
        for field in ("session_open", "session_high", "session_low", "previous_close"):
            value = getattr(self, field)
            if value is not None and value < 0:
                raise ValueError(f"{field} cannot be negative")
        if self.trailing_percentile_252 is not None and not (
            Decimal(0) <= self.trailing_percentile_252 <= Decimal(100)
        ):
            raise ValueError("trailing percentile must be between 0 and 100")
        if not 0 <= self.history_sample_size <= 252:
            raise ValueError("history_sample_size must be between 0 and 252")
        if self.history_sample_size == 0:
            if any(
                value is not None
                for value in (
                    self.trailing_percentile_252,
                    self.history_start,
                    self.history_end,
                )
            ):
                raise ValueError("empty volatility history cannot expose percentile bounds")
        elif any(value is None for value in (self.history_start, self.history_end)):
            raise ValueError("non-empty volatility history requires date bounds")
        elif (
            self.history_start is not None
            and self.history_end is not None
            and self.history_start > self.history_end
        ):
            raise ValueError("volatility history bounds are reversed")
        if self.expected_horizon_days <= 0:
            raise ValueError("expected_horizon_days must be positive")
        if self.directional:
            raise ValueError("volatility magnitude indices are not directional signals")


@dataclass(frozen=True, slots=True)
class FuturesContractPosition:
    product_code: str
    contract_code: str
    volume: int
    open_interest: int
    open_interest_change: int | None
    last_price: Decimal | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.product_code.strip() or not self.contract_code.strip():
            raise ValueError("futures positioning identity cannot be empty")
        if self.volume < 0 or self.open_interest < 0:
            raise ValueError("futures volume and open interest cannot be negative")
        _require_finite(self.last_price, "last_price")
        if self.last_price is not None and self.last_price <= 0:
            raise ValueError("last_price must be positive")
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class FuturesPositioningContext:
    product_code: str
    contracts: tuple[FuturesContractPosition, ...]
    contract_count: int
    volume: int
    open_interest: int
    open_interest_change: int | None
    open_interest_change_contracts: int
    source: MarketContextSource
    unit: str = "lots"
    counting_method: PositionCountingMethod = PositionCountingMethod.SINGLE_SIDE
    directional_inference: DirectionalInference = DirectionalInference.UNAVAILABLE

    def __post_init__(self) -> None:
        if not self.product_code.strip():
            raise ValueError("positioning product code cannot be empty")
        if not self.contracts:
            raise ValueError("positioning context requires at least one real contract")
        if self.contract_count != len(self.contracts):
            raise ValueError("contract_count does not match contracts")
        if any(item.product_code != self.product_code for item in self.contracts):
            raise ValueError("positioning contracts must belong to one product")
        if self.volume != sum(item.volume for item in self.contracts):
            raise ValueError("aggregate volume does not match contracts")
        if self.open_interest != sum(item.open_interest for item in self.contracts):
            raise ValueError("aggregate open interest does not match contracts")
        available_changes = tuple(
            item.open_interest_change
            for item in self.contracts
            if item.open_interest_change is not None
        )
        if self.open_interest_change_contracts != len(available_changes):
            raise ValueError("open interest change coverage does not match contracts")
        if len(available_changes) == len(self.contracts):
            if self.open_interest_change != sum(available_changes):
                raise ValueError("aggregate open interest change does not match contracts")
        elif self.open_interest_change is not None:
            raise ValueError("partial open interest changes cannot be presented as a total")
        if self.unit != "lots":
            raise ValueError("futures positioning unit must be lots")
        if self.counting_method is not PositionCountingMethod.SINGLE_SIDE:
            raise ValueError("SHFE public positioning must retain its single-side counting basis")
        if self.directional_inference is not DirectionalInference.UNAVAILABLE:
            raise ValueError("aggregate open interest cannot identify long or short direction")
