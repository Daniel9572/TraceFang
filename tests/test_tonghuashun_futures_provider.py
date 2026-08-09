from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from market_analysis.infrastructure.providers.tonghuashun_futures import (
    TonghuashunFuturesProvider,
    TonghuashunFuturesSettings,
    TonghuashunFuturesSymbolMapper,
)
from market_analysis.instruments import (
    BRENT_CRUDE_CONTINUOUS,
    NASDAQ_COMPOSITE,
    SHFE_GOLD_2610,
    SHFE_GOLD_WEIGHTED,
    SHFE_SILVER_2706,
    SHFE_SILVER_WEIGHTED,
    SSE_COMPOSITE,
    US_DOLLAR_INDEX,
)


def jsonp(callback: str, payload: dict) -> str:
    return f"{callback}({json.dumps(payload, ensure_ascii=True)})"


def time_payload(symbol: str) -> dict:
    if symbol == "88_IXIC":
        return {
            symbol: {
                "name": "纳斯达克综合指数",
                "pre": "26348.350",
                "date": "20260807",
                "dates": ["20260806", "20260807"],
                "tradeTime": ["2130-0400"],
                "data": (
                    "2130,26562.100,0,26562.100,861418000;"
                    "0359,26687.160,0,26687.160,851925100;"
                    "0400,26690.620,0,26690.620,851415700"
                ),
            }
        }
    gold = symbol == "qh_au8888"
    return {
        symbol: {
            "name": "沪金加权" if gold else "沪银加权",
            "pre": "928.80" if gold else "15217",
            "date": "20260810",
            "dates": ["20260807", "20260808", "20260810"],
            "tradeTime": ["2100-0230", "0900-1015", "1030-1130", "1330-1500"],
            "data": (
                "2100,950.82,720803620,950.57,758;"
                "0229,942.69,240270000,946.14,255;"
                "0230,942.73,420140000,946.13,446"
                if gold
                else "2100,15892,585799820,15885,2455;"
                "0229,15484,173740000,15623,748;"
                "0230,15465,554160000,15623,2388"
            ),
        }
    }


def daily_payload(symbol: str) -> dict:
    if symbol == "88_IXIC":
        return {
            "name": "纳斯达克综合指数",
            "data": (
                "20260806,26302.950,26389.770,26185.400,26348.350,7012345600,,,,,0;"
                "20260807,26534.660,26712.620,26478.010,26690.620,7191029200,,,,,0"
            ),
        }
    gold = symbol == "qh_au8888"
    return {
        "name": "沪金加权" if gold else "沪银加权",
        "data": (
            "20260807,926.56,938.04,922.09,937.83,365375,339072030000.00,,"
            "926.32,,0;"
            "20260810,950.57,951.86,941.85,942.73,242737,229474700000.00,,"
            "928.80,,0"
            if gold
            else "20260807,15044,15658,14960,15647,1038491,236934830000,,"
            "15230,,0;"
            "20260810,15885,15907,15459,15465,715547,167626140000,,15217,,0"
        ),
    }


def minute_payload(symbol: str) -> dict:
    if symbol == "88_IXIC":
        return {
            "name": "纳斯达克综合指数",
            "data": (
                "202608071559,26686.100,26688.200,26684.900,26687.160,850000000,0.000,,,,0;"
                "202608071600,26687.160,26690.620,26687.160,26690.620,851925100,0.000,,,,0"
            ),
        }
    gold = symbol == "qh_au8888"
    return {
        "name": "沪金加权" if gold else "沪银加权",
        "data": (
            "202608080229,942.80,943.00,942.60,942.69,255,240270000.00,,,0.00,0;"
            "202608080230,942.70,942.81,942.64,942.73,446,420140000.00,,,0.00,0"
            if gold
            else "202608080229,15491,15498,15480,15484,748,173740000,,,0.00,0;"
            "202608080230,15484,15490,15459,15465,2388,554160000,,,0.00,0"
        ),
    }


class TonghuashunFuturesProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            parts = request.url.path.strip("/").split("/")
            if "/time/" in request.url.path:
                symbol = parts[parts.index("time") + 1]
                payload = time_payload(symbol)
            else:
                symbol = parts[parts.index("line") + 1]
                if "/01/" in request.url.path:
                    payload = daily_payload(symbol)
                else:
                    payload = minute_payload(symbol)
                    if request.url.path.endswith("/2026.js"):
                        payload.pop("name")
            callback = "quotebridge_" + "_".join(parts).replace(".", "_")
            return httpx.Response(
                200,
                text=jsonp(callback, payload),
                headers={"Content-Type": "application/javascript"},
            )

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.provider = TonghuashunFuturesProvider(
            TonghuashunFuturesSettings(),
            http_client=self.http,
        )

    async def asyncTearDown(self) -> None:
        await self.http.aclose()

    async def test_maps_all_supported_public_markets(self) -> None:
        mapper = TonghuashunFuturesSymbolMapper()

        expected = {
            SHFE_GOLD_WEIGHTED: "qh_au8888",
            SHFE_SILVER_WEIGHTED: "qh_ag8888",
            SHFE_GOLD_2610: "qh_au2610",
            SHFE_SILVER_2706: "qh_ag2706",
            US_DOLLAR_INDEX: "wh_USDIND",
            BRENT_CRUDE_CONTINUOUS: "219_BRN0Y",
            SSE_COMPOSITE: "zs_1A0001",
            NASDAQ_COMPOSITE: "88_IXIC",
        }

        self.assertEqual(
            {instrument: mapper.to_provider_code(instrument) for instrument in expected},
            expected,
        )
        self.assertEqual(mapper.line_time_zone(NASDAQ_COMPOSITE).key, "America/New_York")
        self.assertEqual(mapper.line_time_zone(US_DOLLAR_INDEX).key, "UTC")
        self.assertEqual(mapper.line_time_zone(BRENT_CRUDE_CONTINUOUS).key, "Europe/London")

    async def test_quote_uses_settlement_change_and_daily_statistics(self) -> None:
        gold = await self.provider.get_quote(SHFE_GOLD_WEIGHTED)
        silver = await self.provider.get_quote(SHFE_SILVER_WEIGHTED)

        self.assertEqual(gold.last, Decimal("942.73"))
        self.assertEqual(gold.open, Decimal("950.57"))
        self.assertEqual(gold.high, Decimal("951.86"))
        self.assertEqual(gold.low, Decimal("941.85"))
        self.assertEqual(gold.volume, Decimal("242737"))
        self.assertEqual(gold.change, Decimal("13.93"))
        self.assertEqual(gold.change_percent, Decimal("1.50"))
        self.assertEqual(gold.source.provider_symbol, "qh_au8888")
        self.assertEqual(
            gold.source.observed_at,
            datetime(2026, 8, 7, 18, 30, tzinfo=UTC),
        )
        self.assertEqual(silver.last, Decimal("15465"))
        self.assertEqual(silver.change, Decimal("248"))
        self.assertEqual(silver.change_percent, Decimal("1.63"))

    async def test_history_filters_year_file_to_requested_window(self) -> None:
        rows = await self.provider.fetch_historical_candles(
            SHFE_GOLD_WEIGHTED,
            start=datetime(2026, 8, 7, 18, 29, tzinfo=UTC),
            count=1,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].open, Decimal("942.80"))
        self.assertEqual(rows[0].close, Decimal("942.69"))
        self.assertEqual(rows[0].source.provider, "tonghuashun_futures")
        self.assertEqual(
            rows[0].source.raw_payload["history_file"],
            "tonghuashun_public_line_61_year",
        )

    async def test_nasdaq_quote_and_lines_use_their_respective_source_clocks(self) -> None:
        quote = await self.provider.get_quote(NASDAQ_COMPOSITE)
        rows = await self.provider.get_candles(NASDAQ_COMPOSITE, count=1)

        self.assertEqual(quote.last, Decimal("26690.620"))
        self.assertEqual(quote.open, Decimal("26534.660"))
        self.assertEqual(
            quote.source.observed_at,
            datetime(2026, 8, 7, 20, 0, tzinfo=UTC),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].close, Decimal("26690.620"))
        self.assertEqual(
            rows[0].open_time,
            datetime(2026, 8, 7, 20, 0, tzinfo=UTC),
        )


if __name__ == "__main__":
    unittest.main()
