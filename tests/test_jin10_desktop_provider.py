import unittest
from datetime import UTC, datetime
from decimal import Decimal

from market_analysis.domain.errors import ProviderDataError
from market_analysis.infrastructure.providers.jin10 import SPOT_GOLD
from market_analysis.infrastructure.providers.jin10_desktop import Jin10DesktopProvider


class Jin10DesktopProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_windows_ocr_decimal_separators(self) -> None:
        self.assertEqual(
            Jin10DesktopProvider.parse_price("4242 · 65", decimal_places=2),
            Decimal("4242.65"),
        )
        self.assertEqual(
            Jin10DesktopProvider.parse_price("61 \uff0e 993", decimal_places=3),
            Decimal("61.993"),
        )

    def test_rejects_unparseable_ocr_text(self) -> None:
        with self.assertRaises(ProviderDataError):
            Jin10DesktopProvider.parse_price("现货黄金", decimal_places=2)

    async def test_normalizes_capture_to_partial_quote(self) -> None:
        captured_at = datetime(2026, 8, 6, tzinfo=UTC)

        def runner(symbol, probe_only):
            self.assertFalse(probe_only)
            self.assertEqual(symbol, "XAUUSD")
            return {
                "success": True,
                "raw_price": "4242 · 65",
                "captured_at": captured_at.isoformat(),
            }

        provider = Jin10DesktopProvider(runner=runner)
        quote = await provider.get_quote(SPOT_GOLD)

        self.assertEqual(quote.last, Decimal("4242.65"))
        self.assertIsNone(quote.open)
        self.assertIsNone(quote.high)
        self.assertIsNone(quote.low)
        self.assertEqual(quote.source.provider, "jin10_desktop")

    async def test_retries_once_after_transient_ocr_data_error(self) -> None:
        captured_at = datetime(2026, 8, 6, tzinfo=UTC)
        payloads = iter(
            (
                {
                    "success": True,
                    "raw_price": "4278 还 3",
                    "captured_at": captured_at.isoformat(),
                },
                {
                    "success": True,
                    "raw_price": "4278 · 03",
                    "captured_at": captured_at.isoformat(),
                },
            )
        )
        calls = 0

        def runner(symbol, probe_only):
            nonlocal calls
            calls += 1
            self.assertFalse(probe_only)
            self.assertEqual(symbol, "XAUUSD")
            return next(payloads)

        provider = Jin10DesktopProvider(runner=runner)
        quote = await provider.get_quote(SPOT_GOLD)

        self.assertEqual(calls, 2)
        self.assertEqual(quote.last, Decimal("4278.03"))


if __name__ == "__main__":
    unittest.main()
