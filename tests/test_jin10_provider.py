import asyncio
import unittest
from datetime import UTC, datetime
from decimal import Decimal

from tracefang.infrastructure.mcp.types import ToolCallResult
from tracefang.infrastructure.providers.jin10.provider import Jin10Provider
from tracefang.infrastructure.providers.jin10.symbols import SPOT_GOLD, SPOT_SILVER
from tracefang.infrastructure.quota import DailyToolBudget


class FakeMcpClient:
    negotiated_version = "2025-11-25"
    session_id = "test-session"

    async def initialize(self):
        return {"protocolVersion": self.negotiated_version}

    async def list_tools(self):
        names = [
            "get_quote",
            "get_kline",
            "list_flash",
            "search_flash",
            "list_news",
            "search_news",
            "get_news",
            "list_calendar",
        ]
        return {"tools": [{"name": name} for name in names]}

    async def list_resources(self):
        return {"resources": [{"uri": "quote://codes"}]}

    async def read_json_resource(self, _uri):
        return {
            "status": 200,
            "message": "",
            "data": [
                {"code": "XAUUSD", "name": "现货黄金"},
                {"code": "XAGUSD", "name": "现货白银"},
                {"code": "EURUSD", "name": "欧元/美元"},
            ],
        }

    async def call_tool(self, name, arguments):
        if name == "get_quote":
            data = {
                "code": arguments["code"],
                "name": "现货黄金",
                "time": "2026-08-06T00:43:04+08:00",
                "open": "4078.31",
                "close": "4245.64",
                "high": "4265.04",
                "low": "4065.52",
                "volume": 151445,
                "ups_price": "168.45",
                "ups_percent": "4.13",
            }
        elif name == "get_kline":
            data = {
                "code": arguments["code"],
                "name": "现货黄金",
                "klines": [
                    {
                        "time": 1785948180,
                        "open": "4245.79",
                        "high": "4246.04",
                        "low": "4244.84",
                        "close": "4245.14",
                        "volume": 68,
                    },
                    {
                        "time": 1785948120,
                        "open": "4245.60",
                        "high": "4246.23",
                        "low": "4245.14",
                        "close": "4245.91",
                        "volume": 159,
                    },
                ],
            }
        else:
            raise AssertionError(name)
        return ToolCallResult(
            structured_content={"status": 200, "message": "", "data": data},
            content=({"type": "text", "text": "ignored"},),
        )

    async def close(self):
        return None


class ParallelDiscoveryMcpClient(FakeMcpClient):
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0

    async def _track(self, value):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        return value

    async def list_tools(self):
        return await self._track(await super().list_tools())

    async def list_resources(self):
        return await self._track(await super().list_resources())


class Jin10ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = Jin10Provider(
            FakeMcpClient(),
            budget=DailyToolBudget(provider="jin10", daily_limit=1500, reserve=25),
        )
        await self.provider.open()

    async def asyncTearDown(self) -> None:
        await self.provider.close()

    async def test_maps_catalog_without_leaking_unknown_provider_codes(self) -> None:
        entries = await self.provider.list_instruments()
        mapped = {entry.provider_code: entry.instrument for entry in entries}
        self.assertEqual(mapped["XAUUSD"], SPOT_GOLD)
        self.assertEqual(mapped["XAGUSD"], SPOT_SILVER)
        self.assertIsNone(mapped["EURUSD"])

    async def test_parses_quote_from_structured_content(self) -> None:
        quote = await self.provider.get_quote(SPOT_GOLD)
        self.assertEqual(quote.last, Decimal("4245.64"))
        self.assertEqual(quote.source.provider_symbol, "XAUUSD")
        self.assertEqual(quote.source.observed_at.utcoffset().total_seconds(), 8 * 3600)

    async def test_normalizes_kline_to_ascending_time(self) -> None:
        candles = await self.provider.get_candles(
            SPOT_GOLD, start=datetime(2026, 8, 6, tzinfo=UTC), count=2
        )
        self.assertEqual(len(candles), 2)
        self.assertLess(candles[0].open_time, candles[1].open_time)

    async def test_discovers_tools_and_resources_in_parallel(self) -> None:
        client = ParallelDiscoveryMcpClient()
        provider = Jin10Provider(client)
        try:
            await provider.open()
            self.assertEqual(client.max_in_flight, 2)
        finally:
            await provider.close()


if __name__ == "__main__":
    unittest.main()
