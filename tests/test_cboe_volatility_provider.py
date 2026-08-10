from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

import httpx

from market_analysis.domain.errors import ProviderDataError
from market_analysis.infrastructure.providers.cboe_volatility import (
    CboeVolatilityProvider,
)


class CboeVolatilityProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.calls: dict[str, int] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            self.calls[path] = self.calls.get(path, 0) + 1
            if path.endswith("/_VIX.json"):
                return httpx.Response(
                    200,
                    json={
                        "timestamp": "2026-08-10 18:04:36",
                        "symbol": "_VIX",
                        "data": {
                            "symbol": "^VIX",
                            "current_price": 200,
                            "price_change": 2,
                            "price_change_percent": 1,
                            "open": 198,
                            "high": 202,
                            "low": 197,
                            "prev_day_close": 198,
                            "last_trade_time": "2026-08-10T12:49:31",
                        },
                    },
                )
            if path.endswith("/VIX_History.csv"):
                start = date(2025, 1, 1)
                lines = ["DATE,OPEN,HIGH,LOW,CLOSE"]
                for offset in range(252):
                    day = start + timedelta(days=offset)
                    value = offset + 1
                    lines.append(f"{day:%m/%d/%Y},{value},{value},{value},{value}")
                return httpx.Response(200, text="\n".join(lines))
            if path.endswith("/_GVZ.json"):
                return httpx.Response(
                    200,
                    json={
                        "timestamp": "2026-08-10 18:05:18",
                        "symbol": "_GVZ",
                        "data": {
                            "symbol": "^GVZ",
                            "current_price": 25,
                            "last_trade_time": "2026-08-10T13:05:01-05:00",
                        },
                    },
                )
            if path.endswith("/GVZ_History.csv"):
                return httpx.Response(
                    200,
                    text="DATE,GVZ\n08/06/2026,20\n08/07/2026,30\n",
                )
            return httpx.Response(404)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.provider = CboeVolatilityProvider(
            http_client=self.http,
            utc_clock=lambda: datetime(2026, 8, 10, 18, 5, 30, tzinfo=UTC),
        )

    async def asyncTearDown(self) -> None:
        await self.http.aclose()

    async def test_builds_cached_vix_context_with_trailing_252_day_percentile(self) -> None:
        first = await self.provider.get_context("vix")
        second = await self.provider.get_context("VIX")

        self.assertEqual(first, second)
        self.assertEqual(first.index_code, "VIX")
        self.assertEqual(first.underlying, "SPX")
        self.assertEqual(str(first.trailing_percentile_252), "79.37")
        self.assertEqual(first.history_sample_size, 252)
        self.assertFalse(first.directional)
        self.assertTrue(first.source.delayed)
        self.assertEqual(first.source.declared_delay, timedelta(minutes=15))
        self.assertEqual(first.source.observed_at, datetime(2026, 8, 10, 17, 49, 31, tzinfo=UTC))
        self.assertEqual(self.calls["/api/global/delayed_quotes/quotes/_VIX.json"], 1)
        self.assertEqual(
            self.calls["/api/global/us_indices/daily_prices/VIX_History.csv"],
            1,
        )

    async def test_eod_context_reads_history_only_and_includes_latest_value_in_rank(self) -> None:
        first = await self.provider.get_eod_context("VIX")
        second = await self.provider.get_eod_context("vix")

        self.assertEqual(first, second)
        self.assertEqual(first.value, 252)
        self.assertEqual(first.source.as_of, date(2025, 9, 9))
        self.assertEqual(first.source.frequency, "daily_eod")
        self.assertEqual(first.source.received_at, datetime(2026, 8, 10, 18, 5, 30, tzinfo=UTC))
        self.assertEqual(first.trailing_percentile_252, 100)
        self.assertEqual(first.history_sample_size, 252)
        self.assertFalse(first.directional)
        self.assertNotIn("/api/global/delayed_quotes/quotes/_VIX.json", self.calls)
        self.assertEqual(
            self.calls["/api/global/us_indices/daily_prices/VIX_History.csv"],
            1,
        )

    async def test_supports_gvz_single_value_history_and_offset_timestamp(self) -> None:
        context = await self.provider.get_context("GVZ")

        self.assertEqual(context.underlying, "GLD")
        self.assertEqual(str(context.trailing_percentile_252), "50.00")
        self.assertEqual(context.history_sample_size, 2)
        self.assertEqual(context.source.observed_at, datetime(2026, 8, 10, 18, 5, 1, tzinfo=UTC))

    async def test_rejects_a_mismatched_delayed_quote_symbol(self) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "timestamp": "2026-08-10 18:04:36",
                    "symbol": "_GVZ",
                    "data": {
                        "symbol": "^GVZ",
                        "current_price": 20,
                        "last_trade_time": "2026-08-10T13:00:00-05:00",
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = CboeVolatilityProvider(http_client=client)
            with self.assertRaises(ProviderDataError):
                await provider.get_context("VIX")


if __name__ == "__main__":
    unittest.main()
