from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _require_non_negative(value: Decimal | None, field: str) -> None:
    if value is None:
        return
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    if value < 0:
        raise ValueError(f"{field} cannot be negative")


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class OptionDeliveryMode(StrEnum):
    LIVE = "live"
    EXCHANGE_DELAYED = "exchange_delayed"
    END_OF_DAY = "end_of_day"


@dataclass(frozen=True, slots=True)
class OptionContractQuote:
    contract_id: str
    underlying_contract_id: str
    expiry: date
    strike: Decimal
    option_type: OptionType
    contract_multiplier: Decimal
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    previous_settlement: Decimal | None
    volume: int
    open_interest: int
    open_interest_change: int
    turnover: Decimal | None
    observed_at: datetime
    delta: Decimal | None = None
    delta_as_of: date | None = None

    def __post_init__(self) -> None:
        if not self.contract_id.strip() or not self.underlying_contract_id.strip():
            raise ValueError("option contract ids cannot be empty")
        if not self.strike.is_finite() or self.strike <= 0:
            raise ValueError("option strike must be positive and finite")
        if not self.contract_multiplier.is_finite() or self.contract_multiplier <= 0:
            raise ValueError("option contract multiplier must be positive and finite")
        for field in ("bid", "ask", "last", "previous_settlement", "turnover"):
            _require_non_negative(getattr(self, field), field)
        if min(self.volume, self.open_interest) < 0:
            raise ValueError("option volume and open interest cannot be negative")
        if self.delta is not None and (
            not self.delta.is_finite() or not Decimal("-1") <= self.delta <= Decimal("1")
        ):
            raise ValueError("option delta must be finite and within [-1, 1]")
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class OptionUnderlyingQuote:
    contract_id: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    previous_settlement: Decimal | None
    volume: int
    open_interest: int
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.contract_id.strip():
            raise ValueError("underlying contract id cannot be empty")
        for field in ("bid", "ask", "last", "previous_settlement"):
            _require_non_negative(getattr(self, field), field)
        if min(self.volume, self.open_interest) < 0:
            raise ValueError("underlying volume and open interest cannot be negative")
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    provider_id: str
    market_id: str
    market_label: str
    delivery_mode: OptionDeliveryMode
    trading_day: date
    reference_data_as_of: date | None
    observed_at: datetime
    retrieved_at: datetime
    quote_currency: str
    price_unit: str
    quotes: tuple[OptionContractQuote, ...]
    underlyings: Mapping[str, OptionUnderlyingQuote]
    reference_iv_by_underlying: Mapping[str, Decimal]
    source_urls: tuple[str, ...]
    usage_notice: str

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.market_id.strip():
            raise ValueError("option provider and market ids cannot be empty")
        if not self.quotes:
            raise ValueError("option chain cannot be empty")
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.retrieved_at, "retrieved_at")
        for underlying_id, value in self.reference_iv_by_underlying.items():
            if not underlying_id.strip() or not value.is_finite() or value < 0:
                raise ValueError("reference implied volatility must be non-negative and finite")
