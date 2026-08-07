from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from market_analysis.application.quotes import (
    JIN10_CLIENT_SOURCE,
    LatestQuoteCache,
    QuoteQuality,
    QuoteViewService,
)
from market_analysis.domain.errors import ProviderUnavailableError
from market_analysis.domain.models import QuoteSnapshot, SourceMetadata
from market_analysis.infrastructure.providers.jin10 import SPOT_GOLD


def quote(source: str, price: str, *, age_seconds: float = 0) -> QuoteSnapshot:
    received_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return QuoteSnapshot(
        instrument=SPOT_GOLD,
        last=Decimal(price),
        open=Decimal("4200") if source == "jin10_local" else None,
        high=Decimal("4300") if source == "jin10_local" else None,
        low=Decimal("4100") if source == "jin10_local" else None,
        volume=Decimal("12") if source == "jin10_local" else None,
        change=Decimal("2"),
        change_percent=Decimal("0.05"),
        source=SourceMetadata(
            provider=source,
            provider_symbol="XAUUSD.GOODS",
            observed_at=received_at,
            received_at=received_at,
        ),
    )


class QuoteViewServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.loaded: list[str] = []

        async def loader(_instrument, source):
            self.loaded.append(source)
            return None

        self.cache = LatestQuoteCache(loader)
        self.service = QuoteViewService(self.cache, stale_after=lambda _: 10)

    async def test_client_view_exposes_one_aggregated_logical_result(self) -> None:
        web = quote("jin10_web", "4252.34")
        local = quote("jin10_local", "4251.90")
        self.service.accept(web)
        self.service.accept(local)

        view = await self.service.get(SPOT_GOLD, JIN10_CLIENT_SOURCE)

        self.assertEqual(view.source_id, JIN10_CLIENT_SOURCE)
        self.assertEqual(view.quote.source.provider, JIN10_CLIENT_SOURCE)
        self.assertEqual(view.quote.last, web.last)
        self.assertEqual(view.quote.high, local.high)
        self.assertEqual(view.quality, QuoteQuality.COMPLETE)
        self.assertEqual(view.unavailable_fields, ())
        self.assertEqual(view.stale_fields, ())

    async def test_client_view_never_uses_local_price_when_web_is_missing(self) -> None:
        self.service.accept(quote("jin10_local", "4251.90"))

        with self.assertRaisesRegex(ProviderUnavailableError, "实时价格"):
            await self.service.get(SPOT_GOLD, JIN10_CLIENT_SOURCE)

    async def test_client_view_marks_stale_supplement_without_replacing_price(self) -> None:
        web = quote("jin10_web", "4252.34")
        local = quote("jin10_local", "4251.90", age_seconds=20)
        self.service.accept(web)
        self.service.accept(local)

        view = await self.service.get(SPOT_GOLD, JIN10_CLIENT_SOURCE)

        self.assertEqual(view.quote.last, web.last)
        self.assertIsNone(view.quote.high)
        self.assertEqual(view.quality, QuoteQuality.DEGRADED)
        self.assertEqual(
            view.stale_fields,
            ("open", "high", "low", "volume"),
        )

    async def test_query_loads_only_from_local_loader(self) -> None:
        with self.assertRaises(ProviderUnavailableError):
            await self.service.get(SPOT_GOLD, "jin10_mcp")

        self.assertEqual(self.loaded, [])


if __name__ == "__main__":
    unittest.main()
