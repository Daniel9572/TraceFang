from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tracefang.application.futures_positioning import (
    OpenCloseBreakdown,
    OpenInterestParticipation,
    PriceDirection,
    PriceOpenInterestRegime,
    PriceOpenInterestWindow,
    classify_price_open_interest,
)
from tracefang.domain.market_context import DirectionalInference

START = datetime(2026, 8, 11, 1, 0, tzinfo=UTC)


def window(
    price_change: str | None,
    open_interest_change: int | None,
    *,
    contract_count: int = 2,
    covered_contracts: int = 2,
) -> PriceOpenInterestWindow:
    return PriceOpenInterestWindow(
        window_start=START,
        window_end=START + timedelta(days=1),
        price_change=Decimal(price_change) if price_change is not None else None,
        open_interest_change=open_interest_change,
        contract_count=contract_count,
        open_interest_change_contracts=covered_contracts,
    )


class PriceOpenInterestClassificationTests(unittest.TestCase):
    def test_classifies_every_complete_price_and_open_interest_axis(self) -> None:
        cases = (
            ("1", 1, PriceOpenInterestRegime.RISING_WITH_PARTICIPATION_EXPANSION),
            ("-1", 1, PriceOpenInterestRegime.FALLING_WITH_PARTICIPATION_EXPANSION),
            (
                "0",
                1,
                PriceOpenInterestRegime.UNCHANGED_PRICE_WITH_PARTICIPATION_EXPANSION,
            ),
            ("1", -1, PriceOpenInterestRegime.RISING_WITH_POSITION_CONTRACTION),
            ("-1", -1, PriceOpenInterestRegime.FALLING_WITH_POSITION_CONTRACTION),
            (
                "0",
                -1,
                PriceOpenInterestRegime.UNCHANGED_PRICE_WITH_POSITION_CONTRACTION,
            ),
            ("1", 0, PriceOpenInterestRegime.RISING_WITH_UNCHANGED_OPEN_INTEREST),
            ("-1", 0, PriceOpenInterestRegime.FALLING_WITH_UNCHANGED_OPEN_INTEREST),
            ("0", 0, PriceOpenInterestRegime.UNCHANGED),
        )

        for price_change, open_interest_change, expected in cases:
            with self.subTest(price_change=price_change, oi_change=open_interest_change):
                result = classify_price_open_interest(
                    window(price_change, open_interest_change)
                )
                self.assertEqual(result.regime, expected)
                self.assertEqual(result.directional_inference, DirectionalInference.UNAVAILABLE)
                self.assertEqual(result.open_close_breakdown, OpenCloseBreakdown.UNAVAILABLE)

    def test_partial_open_interest_coverage_is_explicitly_unavailable(self) -> None:
        result = classify_price_open_interest(
            window("2.5", None, contract_count=3, covered_contracts=2)
        )

        self.assertEqual(result.price_direction, PriceDirection.RISING)
        self.assertEqual(result.participation, OpenInterestParticipation.UNAVAILABLE)
        self.assertEqual(result.regime, PriceOpenInterestRegime.UNAVAILABLE)
        self.assertEqual(result.open_close_breakdown, OpenCloseBreakdown.UNAVAILABLE)

    def test_missing_price_change_does_not_create_a_regime(self) -> None:
        result = classify_price_open_interest(window(None, 12))

        self.assertEqual(result.price_direction, PriceDirection.UNAVAILABLE)
        self.assertEqual(
            result.participation,
            OpenInterestParticipation.PARTICIPATION_EXPANSION,
        )
        self.assertEqual(result.regime, PriceOpenInterestRegime.UNAVAILABLE)

    def test_rejects_partial_change_presented_as_an_aggregate(self) -> None:
        with self.assertRaisesRegex(ValueError, "partial open interest changes"):
            window("1", 4, contract_count=3, covered_contracts=2)

    def test_rejects_complete_coverage_without_an_aggregate_change(self) -> None:
        with self.assertRaisesRegex(ValueError, "complete open interest coverage"):
            window("1", None)

    def test_rejects_invalid_window_and_non_finite_price_change(self) -> None:
        with self.assertRaisesRegex(ValueError, "window_end"):
            PriceOpenInterestWindow(
                window_start=START,
                window_end=START,
                price_change=Decimal("1"),
                open_interest_change=1,
                contract_count=1,
                open_interest_change_contracts=1,
            )
        with self.assertRaisesRegex(ValueError, "price_change must be finite"):
            window("NaN", 1)

    def test_public_values_never_claim_directional_open_or_close_activity(self) -> None:
        result = classify_price_open_interest(window("1", 1))
        serialized_values = {
            str(result.price_direction),
            str(result.participation),
            str(result.regime),
            str(result.directional_inference),
            str(result.open_close_breakdown),
        }

        for unsupported_label in ("long_open", "long_close", "short_open", "short_close"):
            self.assertNotIn(unsupported_label, serialized_values)


if __name__ == "__main__":
    unittest.main()
