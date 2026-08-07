from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from market_analysis.history_management.admission import HistoricalAdmissionPolicy
from market_analysis.history_management.histdata import HistDataPackageLoader
from market_analysis.history_management.models import (
    AdmissionTarget,
    BarPriceBasis,
    DatasetState,
    HistoricalRecordKind,
    ValidationStatus,
)


class HistDataHistoryPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        package_root = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "history"
            / "packages"
            / "histdata"
            / "2026-07"
        )
        loader = HistDataPackageLoader(package_root)
        cls.gold = loader.load("XAUUSD")
        cls.silver = loader.load("XAGUSD")

    def test_gold_package_is_precise_bar_only_bid_history(self) -> None:
        descriptor = self.gold.bundle.descriptor

        self.assertEqual(self.gold.bundle.record_count, 31_138)
        self.assertEqual(descriptor.record_kind, HistoricalRecordKind.BAR_OBSERVATION)
        self.assertEqual(descriptor.bar_price_basis, BarPriceBasis.BID)
        self.assertEqual(descriptor.bar_interval, timedelta(minutes=1))
        self.assertEqual(descriptor.timestamp_resolution, timedelta(minutes=1))
        self.assertEqual(descriptor.storage_price_scale, 6)
        self.assertEqual(descriptor.effective_price_quantum, Decimal("0.001"))
        self.assertEqual(
            self.gold.bundle.bars[0].open_time,
            datetime(2026, 7, 1, 5, 0, tzinfo=UTC),
        )
        self.assertEqual(self.gold.validation.error_count, 0)
        self.assertEqual(self.gold.validation.status, ValidationStatus.PASS_WITH_WARNINGS)

    def test_silver_package_is_precise_bar_only_bid_history(self) -> None:
        descriptor = self.silver.bundle.descriptor

        self.assertEqual(self.silver.bundle.record_count, 31_040)
        self.assertEqual(descriptor.record_kind, HistoricalRecordKind.BAR_OBSERVATION)
        self.assertEqual(descriptor.bar_price_basis, BarPriceBasis.BID)
        self.assertEqual(descriptor.timestamp_resolution, timedelta(minutes=1))
        self.assertEqual(descriptor.storage_price_scale, 6)
        self.assertEqual(descriptor.effective_price_quantum, Decimal("0.001"))
        self.assertEqual(self.silver.validation.error_count, 0)
        self.assertEqual(self.silver.validation.status, ValidationStatus.PASS_WITH_WARNINGS)

    def test_verified_histdata_package_remains_candidate_without_exact_corroboration(self) -> None:
        decision = HistoricalAdmissionPolicy().evaluate(
            self.gold.bundle,
            self.gold.validation,
            target=AdmissionTarget.TRUSTED_BAR_REFERENCE,
        )

        self.assertEqual(decision.resulting_state, DatasetState.VALIDATED_CANDIDATE)
        self.assertIn("INSUFFICIENT_INDEPENDENT_EXACT_CONFIRMATION", decision.blockers)

    def test_histdata_bar_package_cannot_fill_quote_event_history(self) -> None:
        decision = HistoricalAdmissionPolicy().evaluate(
            self.gold.bundle,
            self.gold.validation,
            target=AdmissionTarget.TRUSTED_QUOTE_HISTORY,
        )

        self.assertFalse(decision.accepted)
        self.assertIn("TARGET_RECORD_KIND_MISMATCH", decision.blockers)


if __name__ == "__main__":
    unittest.main()
