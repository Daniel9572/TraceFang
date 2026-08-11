from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from market_analysis.domain.market_context import DirectionalInference


class PriceDirection(StrEnum):
    RISING = "rising"
    FALLING = "falling"
    UNCHANGED = "unchanged"
    UNAVAILABLE = "unavailable"


class OpenInterestParticipation(StrEnum):
    PARTICIPATION_EXPANSION = "participation_expansion"
    POSITION_CONTRACTION = "position_contraction"
    UNCHANGED = "unchanged"
    UNAVAILABLE = "unavailable"


class PriceOpenInterestRegime(StrEnum):
    RISING_WITH_PARTICIPATION_EXPANSION = "rising_with_participation_expansion"
    FALLING_WITH_PARTICIPATION_EXPANSION = "falling_with_participation_expansion"
    UNCHANGED_PRICE_WITH_PARTICIPATION_EXPANSION = (
        "unchanged_price_with_participation_expansion"
    )
    RISING_WITH_POSITION_CONTRACTION = "rising_with_position_contraction"
    FALLING_WITH_POSITION_CONTRACTION = "falling_with_position_contraction"
    UNCHANGED_PRICE_WITH_POSITION_CONTRACTION = "unchanged_price_with_position_contraction"
    RISING_WITH_UNCHANGED_OPEN_INTEREST = "rising_with_unchanged_open_interest"
    FALLING_WITH_UNCHANGED_OPEN_INTEREST = "falling_with_unchanged_open_interest"
    UNCHANGED = "unchanged"
    UNAVAILABLE = "unavailable"


class OpenCloseBreakdown(StrEnum):
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class PriceOpenInterestWindow:
    """One already-aligned price and aggregate-OI observation window.

    ``open_interest_change`` is a usable total only when every included contract
    contributes the same-window change. Partial coverage must remain ``None``.
    """

    window_start: datetime
    window_end: datetime
    price_change: Decimal | None
    open_interest_change: int | None
    contract_count: int
    open_interest_change_contracts: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.window_start, "window_start"),
            (self.window_end, "window_end"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field} must be timezone-aware")
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be later than window_start")
        if self.price_change is not None and not self.price_change.is_finite():
            raise ValueError("price_change must be finite")
        if self.contract_count < 1:
            raise ValueError("contract_count must be positive")
        if not 0 <= self.open_interest_change_contracts <= self.contract_count:
            raise ValueError("open interest change coverage is invalid")
        complete = self.open_interest_change_contracts == self.contract_count
        if complete and self.open_interest_change is None:
            raise ValueError("complete open interest coverage requires an aggregate change")
        if not complete and self.open_interest_change is not None:
            raise ValueError("partial open interest changes cannot be presented as a total")


@dataclass(frozen=True, slots=True)
class PriceOpenInterestEvaluation:
    window_start: datetime
    window_end: datetime
    price_change: Decimal | None
    open_interest_change: int | None
    price_direction: PriceDirection
    participation: OpenInterestParticipation
    regime: PriceOpenInterestRegime
    directional_inference: DirectionalInference = DirectionalInference.UNAVAILABLE
    open_close_breakdown: OpenCloseBreakdown = OpenCloseBreakdown.UNAVAILABLE


_REGIME_BY_AXES = {
    (
        PriceDirection.RISING,
        OpenInterestParticipation.PARTICIPATION_EXPANSION,
    ): PriceOpenInterestRegime.RISING_WITH_PARTICIPATION_EXPANSION,
    (
        PriceDirection.FALLING,
        OpenInterestParticipation.PARTICIPATION_EXPANSION,
    ): PriceOpenInterestRegime.FALLING_WITH_PARTICIPATION_EXPANSION,
    (
        PriceDirection.UNCHANGED,
        OpenInterestParticipation.PARTICIPATION_EXPANSION,
    ): PriceOpenInterestRegime.UNCHANGED_PRICE_WITH_PARTICIPATION_EXPANSION,
    (
        PriceDirection.RISING,
        OpenInterestParticipation.POSITION_CONTRACTION,
    ): PriceOpenInterestRegime.RISING_WITH_POSITION_CONTRACTION,
    (
        PriceDirection.FALLING,
        OpenInterestParticipation.POSITION_CONTRACTION,
    ): PriceOpenInterestRegime.FALLING_WITH_POSITION_CONTRACTION,
    (
        PriceDirection.UNCHANGED,
        OpenInterestParticipation.POSITION_CONTRACTION,
    ): PriceOpenInterestRegime.UNCHANGED_PRICE_WITH_POSITION_CONTRACTION,
    (
        PriceDirection.RISING,
        OpenInterestParticipation.UNCHANGED,
    ): PriceOpenInterestRegime.RISING_WITH_UNCHANGED_OPEN_INTEREST,
    (
        PriceDirection.FALLING,
        OpenInterestParticipation.UNCHANGED,
    ): PriceOpenInterestRegime.FALLING_WITH_UNCHANGED_OPEN_INTEREST,
    (
        PriceDirection.UNCHANGED,
        OpenInterestParticipation.UNCHANGED,
    ): PriceOpenInterestRegime.UNCHANGED,
}


def classify_price_open_interest(
    value: PriceOpenInterestWindow,
) -> PriceOpenInterestEvaluation:
    """Classifies aggregate participation without inventing position direction.

    Aggregate price and open-interest changes cannot identify long/short opening
    or closing activity. Those fields therefore remain unavailable for every
    result, including fully covered observations.
    """

    if value.price_change is None:
        price_direction = PriceDirection.UNAVAILABLE
    elif value.price_change > 0:
        price_direction = PriceDirection.RISING
    elif value.price_change < 0:
        price_direction = PriceDirection.FALLING
    else:
        price_direction = PriceDirection.UNCHANGED

    if value.open_interest_change is None:
        participation = OpenInterestParticipation.UNAVAILABLE
    elif value.open_interest_change > 0:
        participation = OpenInterestParticipation.PARTICIPATION_EXPANSION
    elif value.open_interest_change < 0:
        participation = OpenInterestParticipation.POSITION_CONTRACTION
    else:
        participation = OpenInterestParticipation.UNCHANGED

    regime = _REGIME_BY_AXES.get(
        (price_direction, participation),
        PriceOpenInterestRegime.UNAVAILABLE,
    )
    return PriceOpenInterestEvaluation(
        window_start=value.window_start,
        window_end=value.window_end,
        price_change=value.price_change,
        open_interest_change=value.open_interest_change,
        price_direction=price_direction,
        participation=participation,
        regime=regime,
    )
