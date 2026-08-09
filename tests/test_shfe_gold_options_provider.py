from __future__ import annotations

import ssl
import unittest
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from market_analysis.application.options import GoldOptionsService
from market_analysis.domain.options import OptionDeliveryMode, OptionType
from market_analysis.infrastructure.providers.shfe_options import (
    ShfeGoldOptionsProvider,
    ShfeGoldOptionsSettings,
)
from market_analysis.infrastructure.providers.shfe_options.provider import (
    create_shfe_tls_context,
)


def _contract(contract_id: str) -> dict[str, str]:
    return {
        "INSTRUMENTID": contract_id,
        "OPENDATE": "20260626",
        "PRICETICK": "0.02",
        "EXCHANGEID": "SHFE",
        "SETTLEMENTGROUPID": "00000001",
        "TRADINGDAY": "20260810",
        "COMMODITYNAME": "黄金",
        "EXPIREDATE": "20260825",
        "COMMODITYID": "au",
        "TRADEUNIT": "1000",
    }


def _option_row(
    contract_id: str,
    *,
    last: str,
    volume: str,
    open_interest: str,
) -> dict[str, str]:
    return {
        "closeprice": "",
        "settlementprice": "",
        "volume": volume,
        "openinterest": open_interest,
        "presettlementprice": last,
        "openprice": last,
        "highprice": last,
        "lowerprice": last,
        "contractname": contract_id,
        "instrumentid": "au",
        "lastprice": last,
        "upperdown": "0",
        "bidvolume": "2",
        "askvolume": "3",
        "updatetime": "2026-08-08 02:30:00",
        "turnover": "10000",
        "bidprice": str(Decimal(last) - Decimal("0.02")),
        "askprice": str(Decimal(last) + Decimal("0.02")),
        "openinterestchg": "1",
    }


class ShfeGoldOptionsProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_tls_context_keeps_peer_and_hostname_verification_enabled(self) -> None:
        context = create_shfe_tls_context()

        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    async def asyncSetUp(self) -> None:
        self.request_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            self.request_count += 1
            path = request.url.path
            if path.endswith("currentTradingday.dat"):
                payload = {
                    "currentTradingday": "20260810",
                    "lastTradingday": "20260807",
                }
            elif path.endswith("delaymarket_auQ.dat"):
                payload = {
                    "delaymarket": [
                        _option_row("au2609C920", last="28", volume="10", open_interest="100"),
                        _option_row("au2609C940", last="15", volume="20", open_interest="300"),
                        _option_row("au2609P920", last="12", volume="40", open_interest="400"),
                        _option_row("au2609P940", last="22", volume="5", open_interest="50"),
                    ]
                }
            elif path.endswith("delaymarket_au.dat"):
                payload = {
                    "delaymarket": [
                        {
                            "contractname": "au2609",
                            "lastprice": "934",
                            "bidprice": "933.98",
                            "askprice": "934.02",
                            "presettlementprice": "928",
                            "volume": "1000",
                            "openinterest": "2500",
                            "updatetime": "2026-08-08 02:30:00",
                        }
                    ]
                }
            elif "ContractBaseInfo20260810.dat" in path:
                payload = {
                    "OptionContractBaseInfo": [
                        _contract(contract_id)
                        for contract_id in (
                            "au2609C920",
                            "au2609C940",
                            "au2609P920",
                            "au2609P940",
                        )
                    ]
                }
            elif path.endswith("kx20260807.dat"):
                deltas = {
                    "au2609C920": "0.62",
                    "au2609C940": "0.48",
                    "au2609P920": "-0.38",
                    "au2609P940": "-1.000001",
                }
                payload = {
                    "o_curinstrument": [
                        {"INSTRUMENTID": key, "DELTA": value}
                        for key, value in deltas.items()
                    ],
                    "o_cursigma": [
                        {
                            "INSTRUMENTID": "au2609",
                            "PRODUCTID": "au_o",
                            "SIGMA": "0.27",
                        }
                    ],
                }
            else:
                return httpx.Response(404)
            return httpx.Response(200, json=payload)

        self.http = httpx.AsyncClient(
            base_url="https://www.shfe.com.cn",
            transport=httpx.MockTransport(handler),
        )
        self.provider = ShfeGoldOptionsProvider(
            ShfeGoldOptionsSettings(snapshot_cache_seconds=30),
            http_client=self.http,
        )

    async def asyncTearDown(self) -> None:
        await self.http.aclose()

    async def test_builds_official_delayed_chain_and_reuses_cache(self) -> None:
        first = await self.provider.get_chain()
        second = await self.provider.get_chain()

        self.assertIs(first, second)
        self.assertEqual(self.request_count, 5)
        self.assertEqual(first.delivery_mode, OptionDeliveryMode.EXCHANGE_DELAYED)
        self.assertEqual(first.observed_at, datetime(2026, 8, 7, 18, 30, tzinfo=UTC))
        self.assertEqual(len(first.quotes), 4)
        self.assertEqual(first.underlyings["au2609"].last, Decimal("934"))
        self.assertEqual(first.reference_iv_by_underlying["au2609"], Decimal("0.27"))
        call = next(item for item in first.quotes if item.contract_id == "au2609C920")
        self.assertEqual(call.option_type, OptionType.CALL)
        self.assertEqual(call.expiry.isoformat(), "2026-08-25")
        self.assertEqual(call.contract_multiplier, Decimal("1000"))
        self.assertEqual(call.delta, Decimal("0.62"))
        self.assertEqual(call.delta_as_of.isoformat(), "2026-08-07")
        put = next(item for item in first.quotes if item.contract_id == "au2609P940")
        self.assertEqual(put.delta, Decimal("-1"))

    async def test_service_derives_positioning_without_fabricating_gex(self) -> None:
        service = GoldOptionsService((self.provider,))

        snapshot = await service.snapshot()

        self.assertTrue(snapshot.available)
        self.assertEqual(snapshot.state, "delayed")
        self.assertEqual(snapshot.provider_id, "shfe_official_delayed")
        self.assertEqual(snapshot.quote_count, 4)
        self.assertEqual(len(snapshot.expiries), 1)
        expiry = snapshot.expiries[0]
        self.assertEqual(expiry.put_call_open_interest_ratio, Decimal("1.1250"))
        self.assertEqual(expiry.put_call_volume_ratio, Decimal("1.5000"))
        self.assertEqual(expiry.call_wall_strike, Decimal("940"))
        self.assertEqual(expiry.put_wall_strike, Decimal("920"))
        self.assertEqual(expiry.max_pain_strike, Decimal("920"))
        self.assertEqual(expiry.reference_iv, Decimal("0.27"))
        self.assertEqual(expiry.delta_coverage_ratio, Decimal("1.0000"))
        self.assertIsNone(expiry.gex)
        self.assertIn("missing_contract_gamma", expiry.gamma_state)
        ai_context = GoldOptionsService.ai_context(snapshot)
        self.assertNotIn("contracts", ai_context)
        self.assertEqual(ai_context["quote_count"], 4)
        self.assertEqual(len(ai_context["expiries"]), 1)
        self.assertEqual(
            {item.market_id for item in snapshot.markets},
            {"shfe_gold_options", "cme_comex_gold_options"},
        )

    def test_settings_reject_non_official_hosts_and_invalid_boolean(self) -> None:
        with self.assertRaisesRegex(ValueError, "official"):
            ShfeGoldOptionsSettings.from_env(
                {"SHFE_GOLD_OPTIONS_BASE_URL": "https://example.com"}
            )
        with self.assertRaisesRegex(ValueError, "boolean"):
            ShfeGoldOptionsSettings.from_env({"SHFE_GOLD_OPTIONS_ENABLED": "sometimes"})


if __name__ == "__main__":
    unittest.main()
