import unittest
from datetime import UTC, datetime
from decimal import Decimal

from tracefang.domain.models import QuoteSnapshot, SourceMetadata
from tracefang.infrastructure.providers.jin10.symbols import SPOT_GOLD


class QuoteSnapshotTests(unittest.TestCase):
    def test_rejects_price_outside_daily_range(self) -> None:
        now = datetime.now(UTC)
        with self.assertRaises(ValueError):
            QuoteSnapshot(
                instrument=SPOT_GOLD,
                last=Decimal("4203"),
                open=Decimal("4200"),
                high=Decimal("4202"),
                low=Decimal("4199"),
                volume=Decimal("1"),
                change=None,
                change_percent=None,
                source=SourceMetadata(
                    provider="test",
                    provider_symbol="GOLD",
                    observed_at=now,
                    received_at=now,
                ),
            )

    def test_rejects_naive_source_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            SourceMetadata(
                provider="test",
                provider_symbol="GOLD",
                observed_at=datetime(2026, 8, 6),
                received_at=datetime.now(UTC),
            )

    def test_accepts_partial_quote_without_daily_range(self) -> None:
        now = datetime.now(UTC)
        quote = QuoteSnapshot(
            instrument=SPOT_GOLD,
            last=Decimal("4203"),
            open=None,
            high=None,
            low=None,
            volume=None,
            change=None,
            change_percent=None,
            source=SourceMetadata(
                provider="desktop",
                provider_symbol="XAUUSD",
                observed_at=now,
                received_at=now,
            ),
        )
        self.assertEqual(quote.last, Decimal("4203"))


if __name__ == "__main__":
    unittest.main()
