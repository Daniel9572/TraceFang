from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from market_analysis import api
from market_analysis.domain.market_context import (
    DirectionalInference,
    EndOfDayMarketContextSource,
    FuturesContractPosition,
    FuturesPositioningContext,
    MarketContextSource,
    PositionCountingMethod,
    VolatilityIndexEodContext,
)


def volatility_context(index_code: str, value: str) -> VolatilityIndexEodContext:
    as_of = date(2026, 8, 7)
    return VolatilityIndexEodContext(
        index_code=index_code,
        underlying="SPX" if index_code == "VIX" else "GLD",
        value=Decimal(value),
        trailing_percentile_252=Decimal("74.21"),
        history_sample_size=252,
        history_start=date(2025, 8, 8),
        history_end=as_of,
        source=EndOfDayMarketContextSource(
            provider_id="cboe_volatility",
            dataset_id=f"{index_code}_History.csv",
            source_url=f"https://example.test/{index_code}_History.csv",
            as_of=as_of,
            received_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        ),
    )


def positioning_context() -> FuturesPositioningContext:
    observed_at = datetime(2026, 8, 11, 1, 36, 3, tzinfo=UTC)
    contracts = (
        FuturesContractPosition(
            product_code="AU",
            contract_code="au2608",
            volume=18,
            open_interest=2949,
            open_interest_change=None,
            last_price=Decimal("948.40"),
            observed_at=observed_at,
        ),
    )
    return FuturesPositioningContext(
        product_code="AU",
        contracts=contracts,
        contract_count=1,
        volume=18,
        open_interest=2949,
        open_interest_change=None,
        open_interest_change_contracts=0,
        source=MarketContextSource(
            provider_id="shfe_positioning",
            dataset_id="delaymarket_au.dat",
            source_url="https://example.test/delaymarket_au.dat",
            observed_at=observed_at,
            received_at=observed_at + timedelta(minutes=30),
            published_at=None,
            delayed=True,
            declared_delay=timedelta(minutes=30),
        ),
        counting_method=PositionCountingMethod.SINGLE_SIDE,
        directional_inference=DirectionalInference.UNAVAILABLE,
    )


class _CboeProvider:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(history_cache_ttl_seconds=21_600)
        self.calls: list[str] = []
        self.values = {
            "VIX": volatility_context("VIX", "18.25"),
            "GVZ": volatility_context("GVZ", "24.75"),
        }

    async def get_eod_context(self, index_code: str) -> VolatilityIndexEodContext:
        self.calls.append(index_code)
        return self.values[index_code]


class _ShfeProvider:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(cache_ttl_seconds=60)
        self.calls: list[str] = []

    async def get_context(self, product_code: str) -> FuturesPositioningContext:
        self.calls.append(product_code)
        return positioning_context()


class ExpertContextApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_volatility_endpoint_is_history_only_eod_and_non_directional(self) -> None:
        provider = _CboeProvider()
        with patch.object(api, "_cboe_volatility", return_value=provider):
            payload = await api.expert_volatility_context()

        self.assertEqual(provider.calls, ["VIX", "GVZ"])
        self.assertEqual(payload["contract_version"], "volatility-eod-context-v1")
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["mode"], "eod")
        self.assertEqual(payload["refresh_after_seconds"], 21_600)
        self.assertFalse(payload["directional"])
        self.assertEqual(payload["indices"][0]["value"], 18.25)
        self.assertEqual(payload["indices"][0]["as_of"], "2026-08-07")
        self.assertEqual(payload["indices"][0]["source"]["frequency"], "daily_eod")
        self.assertNotIn("delayed", payload["indices"][0]["source"])

    async def test_shfe_endpoint_preserves_delay_counting_and_missing_delta_oi(self) -> None:
        provider = _ShfeProvider()
        with patch.object(api, "_shfe_positioning", return_value=provider):
            payload = await api.expert_shfe_positioning("au")

        self.assertEqual(provider.calls, ["au"])
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["mode"], "delayed_snapshot")
        self.assertTrue(payload["delayed"])
        self.assertEqual(payload["declared_delay_seconds"], 1800)
        self.assertEqual(payload["counting_method"], "single_side")
        self.assertEqual(payload["directional_inference"], "unavailable")
        self.assertTrue(payload["derived_aggregate"])
        self.assertIsNone(payload["open_interest_change"])
        self.assertEqual(payload["open_interest_change_contracts"], 0)
        self.assertIsNone(payload["contracts"][0]["open_interest_change"])
        self.assertEqual(payload["contracts"][0]["last_price"], 948.4)

    async def test_context_provider_cleanup_closes_both_and_clears_runtime(self) -> None:
        cboe = SimpleNamespace(aclose=AsyncMock())
        shfe = SimpleNamespace(aclose=AsyncMock())
        with (
            patch.object(api.runtime, "cboe_volatility", cboe),
            patch.object(api.runtime, "shfe_positioning", shfe),
        ):
            await api._close_expert_context_providers()

            self.assertIsNone(api.runtime.cboe_volatility)
            self.assertIsNone(api.runtime.shfe_positioning)
        cboe.aclose.assert_awaited_once_with()
        shfe.aclose.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
