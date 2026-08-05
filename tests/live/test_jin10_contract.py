import os
import unittest

from market_analysis.infrastructure.mcp import StreamableHttpMcpClient
from market_analysis.infrastructure.providers.jin10 import (
    SPOT_GOLD,
    SPOT_SILVER,
    Jin10Provider,
    Jin10Settings,
)


@unittest.skipUnless(
    os.environ.get("RUN_JIN10_LIVE") == "1" and os.environ.get("JIN10_MCP_BEARER_TOKEN"),
    "set RUN_JIN10_LIVE=1 and JIN10_MCP_BEARER_TOKEN to run",
)
class Jin10LiveContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_gold_silver_quotes_and_gold_kline(self) -> None:
        settings = Jin10Settings.from_env()
        client = StreamableHttpMcpClient(
            endpoint=settings.endpoint,
            bearer_token=settings.bearer_token,
            timeout_seconds=settings.timeout_seconds,
        )
        provider = Jin10Provider(client)
        async with provider:
            catalog = await provider.list_instruments()
            mapped_codes = {
                entry.provider_code for entry in catalog if entry.instrument is not None
            }
            self.assertTrue({"XAUUSD", "XAGUSD"}.issubset(mapped_codes))

            gold = await provider.get_quote(SPOT_GOLD)
            silver = await provider.get_quote(SPOT_SILVER)
            candles = await provider.get_candles(SPOT_GOLD, count=3)

            self.assertEqual(gold.source.provider_symbol, "XAUUSD")
            self.assertEqual(silver.source.provider_symbol, "XAGUSD")
            self.assertEqual(len(candles), 3)
            self.assertEqual(
                tuple(sorted(c.open_time for c in candles)),
                tuple(c.open_time for c in candles),
            )


if __name__ == "__main__":
    unittest.main()
