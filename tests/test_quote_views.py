from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tracefang.application.quotes import (
    JIN10_CLIENT_SOURCE,
    TONGHUASHUN_FUTURES_SOURCE,
    LatestQuoteCache,
    QuoteQuality,
    QuoteViewService,
)
from tracefang.domain.errors import ProviderUnavailableError
from tracefang.domain.models import Instrument, QuoteSnapshot, SourceMetadata
from tracefang.infrastructure.providers.jin10 import SPOT_GOLD
from tracefang.instruments import (
    SHFE_GOLD_WEIGHTED,
    SPOT_GOLD_CNH_PER_GRAM,
    TROY_OUNCE_GRAMS,
    USD_CNH,
)


def quote(
    source: str,
    price: str,
    *,
    instrument: Instrument = SPOT_GOLD,
    change: str | None = "2",
    age_seconds: float = 0,
) -> QuoteSnapshot:
    received_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return QuoteSnapshot(
        instrument=instrument,
        last=Decimal(price),
        open=Decimal("4200") if source == "jin10_local" else None,
        high=Decimal("4300") if source == "jin10_local" else None,
        low=Decimal("4100") if source == "jin10_local" else None,
        volume=Decimal("12") if source == "jin10_local" else None,
        change=Decimal(change) if change is not None else None,
        change_percent=Decimal("0.05") if change is not None else None,
        source=SourceMetadata(
            provider=source,
            provider_symbol=("USDCNH.FXCM" if instrument == USD_CNH else "XAUUSD.GOODS"),
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

    async def test_client_view_exposes_one_aggregated_realtime_result(self) -> None:
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

    async def test_direct_futures_source_preserves_complete_weighted_quote(self) -> None:
        now = datetime.now(UTC)
        direct = QuoteSnapshot(
            instrument=SHFE_GOLD_WEIGHTED,
            last=Decimal("942.73"),
            open=Decimal("951.22"),
            high=Decimal("951.83"),
            low=Decimal("941.82"),
            volume=Decimal("242737"),
            change=Decimal("13.93"),
            change_percent=Decimal("1.50"),
            source=SourceMetadata(
                provider=TONGHUASHUN_FUTURES_SOURCE,
                provider_symbol="159.aufi",
                observed_at=now,
                received_at=now,
            ),
        )
        self.service.accept(direct)

        view = await self.service.get(
            SHFE_GOLD_WEIGHTED,
            TONGHUASHUN_FUTURES_SOURCE,
        )

        self.assertEqual(view.source_id, TONGHUASHUN_FUTURES_SOURCE)
        self.assertEqual(view.quote.source.provider, TONGHUASHUN_FUTURES_SOURCE)
        self.assertEqual(view.quote.last, Decimal("942.73"))
        self.assertEqual(view.quote.volume, Decimal("242737"))
        self.assertEqual(view.quality, QuoteQuality.COMPLETE)

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

    async def test_last_view_keeps_same_source_values_with_explicit_staleness(self) -> None:
        web = quote("jin10_web", "4252.34", age_seconds=20)
        local = quote("jin10_local", "4251.90", age_seconds=20)
        self.service.accept(web)
        self.service.accept(local)

        with self.assertRaisesRegex(ProviderUnavailableError, "已过期"):
            await self.service.get(SPOT_GOLD, JIN10_CLIENT_SOURCE)

        view = await self.service.get_last(SPOT_GOLD, JIN10_CLIENT_SOURCE)

        self.assertEqual(view.quote.last, web.last)
        self.assertEqual(view.quote.high, local.high)
        self.assertEqual(view.quality, QuoteQuality.DEGRADED)
        self.assertEqual(
            view.stale_fields,
            ("last", "change", "change_percent", "open", "high", "low", "volume"),
        )

    async def test_query_loads_only_from_local_loader(self) -> None:
        with self.assertRaises(ProviderUnavailableError):
            await self.service.get(SPOT_GOLD, "jin10_mcp")

        self.assertEqual(self.loaded, [])

    async def test_derived_gold_uses_both_realtime_legs_and_troy_ounce_conversion(
        self,
    ) -> None:
        gold = quote("jin10_web", "3000", change="10")
        fx = quote(
            "jin10_web",
            "7.2",
            instrument=USD_CNH,
            change="0.01",
        )
        self.service.accept(gold)
        self.service.accept(fx)

        view = await self.service.get(SPOT_GOLD_CNH_PER_GRAM, JIN10_CLIENT_SOURCE)

        expected = gold.last * fx.last / TROY_OUNCE_GRAMS
        previous = (gold.last - gold.change) * (fx.last - fx.change) / TROY_OUNCE_GRAMS
        self.assertEqual(view.quote.last, expected)
        self.assertEqual(view.quote.change, expected - previous)
        self.assertEqual(view.quote.instrument, SPOT_GOLD_CNH_PER_GRAM)
        self.assertEqual(
            view.quote.source.raw_payload["grams_per_troy_ounce"],
            "31.1034768",
        )
        self.assertEqual(view.unavailable_fields, ("open", "high", "low", "volume"))

    async def test_derived_gold_requires_the_realtime_fx_leg(self) -> None:
        self.service.accept(quote("jin10_web", "3000"))

        with self.assertRaisesRegex(ProviderUnavailableError, "实时汇率"):
            await self.service.get(SPOT_GOLD_CNH_PER_GRAM, JIN10_CLIENT_SOURCE)


if __name__ == "__main__":
    unittest.main()
