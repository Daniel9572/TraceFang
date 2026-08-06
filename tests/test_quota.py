import unittest

from market_analysis.domain.errors import ProviderRateLimitError
from market_analysis.infrastructure.providers.jin10.settings import Jin10Settings
from market_analysis.infrastructure.quota import DailyToolBudget


class DailyToolBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_tool_level_daily_usage(self) -> None:
        budget = DailyToolBudget(
            provider="official",
            daily_limit=10,
            reserve=2,
            timezone="Asia/Shanghai",
        )

        await budget.acquire("get_quote")
        await budget.acquire("get_quote")
        quote, kline = await budget.snapshots(("get_quote", "get_kline"))

        self.assertEqual(quote.used, 2)
        self.assertEqual(quote.limit, 10)
        self.assertEqual(quote.reserve, 2)
        self.assertEqual(quote.available, 6)
        self.assertEqual(quote.usage_percent, 20)
        self.assertEqual(quote.period, "daily")
        self.assertEqual(quote.scope, "application_process")
        self.assertEqual(quote.resets_at.hour, 0)
        self.assertEqual(kline.used, 0)

    async def test_reserve_prevents_consuming_the_full_upstream_limit(self) -> None:
        budget = DailyToolBudget(provider="official", daily_limit=3, reserve=1)
        await budget.acquire("get_quote")
        await budget.acquire("get_quote")
        with self.assertRaises(ProviderRateLimitError):
            await budget.acquire("get_quote")


class Jin10QuotaSettingsTests(unittest.TestCase):
    def test_loads_quota_presentation_settings(self) -> None:
        settings = Jin10Settings.from_env(
            {
                "JIN10_MCP_BEARER_TOKEN": "secret",
                "JIN10_MCP_DAILY_TOOL_LIMIT": "1200",
                "JIN10_MCP_QUOTA_RESERVE": "30",
                "JIN10_MCP_QUOTA_TIMEZONE": "Asia/Shanghai",
                "JIN10_MCP_QUOTA_WARNING_PERCENT": "75",
            }
        )
        self.assertEqual(settings.daily_tool_limit, 1200)
        self.assertEqual(settings.quota_reserve, 30)
        self.assertEqual(settings.quota_warning_percent, 75)

    def test_rejects_invalid_warning_threshold(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            Jin10Settings.from_env(
                {
                    "JIN10_MCP_BEARER_TOKEN": "secret",
                    "JIN10_MCP_QUOTA_WARNING_PERCENT": "120",
                }
            )


if __name__ == "__main__":
    unittest.main()
