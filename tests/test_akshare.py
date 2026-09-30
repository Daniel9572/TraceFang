from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pandas as pd
from fastapi import FastAPI

from tracefang.akshare_worker import build_chain, load_bars, normalize_metadata
from tracefang.application.research import (
    ResearchDataService,
    ResearchError,
    ResearchQuery,
    normalize_bars,
)
from tracefang.research_api import research_router, technical_evidence


def metadata_row(code="au3011C900", underlying="au3011", **changes):
    return {
        "交易所ID": "SHFE",
        "合约ID": code,
        "标的合约ID": underlying,
        "最后交易日": "20301025",
        "期权类型": "1",
        "行权价": "900",
        "合约乘数": "1000",
        "交割年份": "2030",
        "交割月份": "11",
        **changes,
    }


class AkshareMappingTests(unittest.TestCase):
    def test_metadata_uses_actual_expiry_and_multiplier_not_contract_month(self):
        result = normalize_metadata(
            [
                metadata_row(**{"交割月份": "10"}),
                metadata_row(
                    "10011425",
                    "510050",
                    **{
                        "交易所ID": "SSE",
                        "最后交易日": "20301127",
                        "行权价": "2.8",
                        "合约乘数": "10000",
                    },
                ),
                metadata_row("expired", **{"最后交易日": "20200101"}),
                metadata_row("invalid", **{"合约乘数": "NaN"}),
            ]
        )
        gold, etf = result["contracts"]
        self.assertEqual(
            (gold["month"], gold["expiry"], gold["multiplier"]), ("203011", "2030-10-25", 1000)
        )
        self.assertEqual(
            (etf["symbol"], etf["underlying"], etf["multiplier"], etf["currency"]),
            ("10011425.SH", "510050.SH", 10000, "CNY"),
        )
        self.assertEqual(result["rejected_rows"], 1)

    def test_minutes_shift_end_label_and_preserve_open_interest_for_ai(self):
        ak = SimpleNamespace(
            futures_zh_minute_sina=Mock(
                return_value=pd.DataFrame(
                    [
                        {
                            "datetime": "2026-09-30 15:00:00",
                            "open": 910,
                            "high": 912,
                            "low": 909,
                            "close": 911,
                            "volume": 12,
                            "hold": 225591,
                        },
                    ]
                )
            )
        )
        query = ResearchQuery(source="akshare", asset="future", symbol="AU0", period="5m")
        rows = load_bars(ak, query.model_dump())
        ak.futures_zh_minute_sina.assert_called_once_with(symbol="AU0", period="5")
        self.assertEqual(rows[0]["time"], "2026-09-30T14:55:00")
        with patch("tracefang.application.research.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 30, 7, 1, tzinfo=UTC)
            bars, rejected = normalize_bars(rows, query)
        self.assertEqual(rejected, 0)
        self.assertEqual(bars[0]["open_time"], "2026-09-30T06:55:00+00:00")
        self.assertEqual(bars[0]["state"], "final")
        self.assertEqual(technical_evidence(bars)["open_interest_last"], 225591)
        rows[0]["open_interest"] = -1
        self.assertIsNone(normalize_bars(rows, query)[0][0]["open_interest"])

    def test_etf_chain_excludes_wrong_strike_or_underlying_and_missing_prices(self):
        metadata = normalize_metadata(
            [
                metadata_row(
                    str(10000000 + index),
                    "510050",
                    **{
                        "交易所ID": "SSE",
                        "行权价": "2.8",
                        "合约乘数": "10000",
                    },
                )
                for index in range(4)
            ]
        )["contracts"]

        def spot(symbol):
            fields = {
                "买价": 0,
                "卖价": 0.141,
                "最新价": 0.14,
                "行权价": 3 if symbol == "10000001" else 2.8,
                "标的股票": "510300" if symbol == "10000002" else "510050",
                "行情时间": "20300930160000",
            }
            if symbol == "10000003":
                fields.update(买价=0.2, 卖价=0.1)
            return pd.DataFrame({"字段": list(fields), "值": list(fields.values())})

        ak = SimpleNamespace(
            option_sse_codes_sina=lambda **kwargs: pd.DataFrame(
                {"期权代码": [str(10000000 + i) for i in range(4)]}
            ),
            option_sse_spot_price_sina=spot,
            option_sse_underlying_spot_price_sina=lambda **kwargs: pd.DataFrame(
                {
                    "字段": ["最近成交价", "行情日期", "行情时间"],
                    "值": [2.9, "2030-09-30", "16:00:00"],
                }
            ),
        )
        chain = build_chain(ak, {"symbol": "510050.SH", "month": "203011", "contracts": metadata})
        self.assertEqual(len(chain["contracts"]), 2)
        first, inverted = chain["contracts"]
        self.assertIsNone(first["bid"])
        self.assertEqual(
            (first["ask"], first["multiplier"], first["currency"]), (0.141, 10000, "CNY")
        )
        self.assertEqual(first["observed_at"], "2030-09-30T16:00:00+08:00")
        self.assertIsNone(inverted["bid"])
        self.assertIsNone(inverted["ask"])
        self.assertEqual(chain["reference_spot"], 2.9)
        self.assertTrue(any("2 条报价" in warning for warning in chain["warnings"]))

    def test_commodity_chain_matches_hyphen_codes_without_inventing_quote_time(self):
        metadata = normalize_metadata(
            [metadata_row("m3011-C-900", "m3011", **{"交易所ID": "DCE", "合约乘数": "10"})]
        )["contracts"]
        ak = SimpleNamespace(
            option_commodity_contract_table_sina=Mock(
                return_value=pd.DataFrame(
                    [
                        {
                            "看涨合约-标识": "m3011-C-900",
                            "行权价": 900,
                            "看涨合约-买价": 30,
                            "看涨合约-卖价": 31,
                            "看涨合约-最新价": 30.5,
                        },
                    ]
                )
            ),
            futures_zh_daily_sina=lambda **kwargs: pd.DataFrame(
                {"date": ["2030-09-30"], "close": [950]}
            ),
        )
        chain = build_chain(ak, {"symbol": "M", "month": "203011", "contracts": metadata})
        self.assertEqual(chain["pricing_model"], "black76")
        self.assertEqual(chain["contracts"][0]["multiplier"], 10)
        self.assertIsNone(chain["contracts"][0]["observed_at"])
        self.assertEqual(chain["reference_observed_at"], "2030-09-30T00:00:00+08:00")

    def test_czce_year_rollover_and_sina_four_digit_alias_preserve_exchange_identity(self):
        metadata = normalize_metadata(
            [
                metadata_row(
                    "SR101C4700",
                    "SR101",
                    **{
                        "交易所ID": "CZCE",
                        "最后交易日": "20301211",
                        "行权价": "4700",
                        "合约乘数": "10",
                        "交割年份": "2030",
                        "交割月份": "12",
                    },
                ),
            ]
        )["contracts"]
        self.assertEqual(metadata[0]["month"], "203101")
        ak = SimpleNamespace(
            option_commodity_contract_table_sina=Mock(
                return_value=pd.DataFrame(
                    [
                        {
                            "看涨合约-看涨期权合约": "sr3101C4700",
                            "行权价": 4700,
                            "看涨合约-买价": 20,
                            "看涨合约-卖价": 21,
                            "看涨合约-最新价": 20.5,
                        },
                    ]
                )
            ),
            futures_zh_daily_sina=Mock(return_value=pd.DataFrame()),
        )
        chain = build_chain(ak, {"symbol": "SR", "month": "203101", "contracts": metadata})
        ak.option_commodity_contract_table_sina.assert_called_once_with(
            symbol="白糖期权", contract="sr3101"
        )
        self.assertEqual(chain["contracts"][0]["symbol"], "SR101C4700")
        self.assertEqual(chain["contracts"][0]["expiry"], "2030-12-11")


class AkshareServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = ResearchDataService(
            Path(self.directory.name) / "research.db", environment={}
        )

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def test_capabilities_reject_unsupported_period_before_network(self):
        for asset, period, symbol in [
            ("option", "5m", "AU3011C900"),
            ("equity", "1m", "600519.SH"),
            ("future", "1w", "AU0"),
            ("index", "1d", "IO"),
        ]:
            with (
                self.subTest(asset=asset, period=period),
                self.assertRaises(ResearchError) as error,
            ):
                self.service.validate(
                    ResearchQuery(source="akshare", asset=asset, symbol=symbol, period=period)
                )
            self.assertEqual(error.exception.status, 422)
        self.service.validate(
            ResearchQuery(source="akshare", asset="future", symbol="AU0", period="1h")
        )

    async def test_resource_requests_share_fetch_and_preserve_stale_identity(self):
        self.service._akshare_call = AsyncMock(return_value={"contracts": ["gold"]})
        first, second = await asyncio.gather(
            *(self.service._akshare_resource("chain", {"symbol": "AU"}, 30) for _ in range(2))
        )
        self.assertEqual(self.service._akshare_call.await_count, 1)
        first["result"]["contracts"].clear()
        self.assertEqual(second["result"]["contracts"], ["gold"])
        cached = await self.service._akshare_resource("chain", {"symbol": "AU"}, 30)
        self.assertEqual(cached["cache_state"], "cached")
        self.service._akshare_call.side_effect = ResearchError("upstream failed")
        stale = await self.service._akshare_resource("chain", {"symbol": "AU"}, 0)
        self.assertEqual(stale["cache_state"], "stale")
        self.assertEqual(stale["result"]["contracts"], ["gold"])
        with self.assertRaises(ResearchError):
            await self.service._akshare_resource("chain", {"symbol": "IO"}, 0)

    async def test_month_directory_keeps_contract_month_distinct_from_expiry(self):
        contracts = normalize_metadata([metadata_row(), metadata_row("cu3011C900", "cu3011")])
        self.service._akshare_call = AsyncMock(return_value=contracts)
        directory = await self.service.akshare_months("AU")
        self.assertEqual(
            directory["months"],
            [{"month": "203011", "expiry": "2030-10-25", "label": "2030-11 · 到期 2030-10-25"}],
        )
        for month in ("203013", "203012"):
            with self.assertRaises(ResearchError):
                await self.service.akshare_option_chain("AU", month)
        self.assertEqual(self.service._akshare_call.await_count, 1)
        self.service._akshare_call.return_value = {
            "contracts": contracts["contracts"][:1],
            "warnings": [],
        }
        result = await self.service.akshare_option_chain("AU", "203011")
        self.assertEqual(result["contracts"][0]["expiry"], "2030-10-25")
        self.service._akshare_call.assert_awaited_with(
            "chain", {"symbol": "AU", "month": "203011", "contracts": directory["contracts"]}
        )

    async def test_cancelling_worker_kills_process_and_reaps_it(self):
        started = asyncio.Event()

        async def communicate(_):
            started.set()
            await asyncio.Future()

        process = SimpleNamespace(
            returncode=None, communicate=communicate, kill=Mock(), wait=AsyncMock()
        )
        with patch(
            "tracefang.application.research.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            task = asyncio.create_task(self.service._akshare_call("bars", {}))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_three_digit_history_requires_verified_year_instead_of_guessing_a_decade(self):
        directory = normalize_metadata(
            [
                metadata_row(
                    "SR101C4700",
                    "SR101",
                    **{
                        "交易所ID": "CZCE",
                        "最后交易日": "20301211",
                        "行权价": "4700",
                        "交割年份": "2030",
                        "交割月份": "12",
                    },
                )
            ]
        )
        self.service._akshare_call = AsyncMock(side_effect=[directory, []])
        await self.service._akshare_bars(
            ResearchQuery(source="akshare", asset="option", symbol="SR101C4700")
        )
        operation, params = self.service._akshare_call.call_args.args
        self.assertEqual(operation, "bars")
        self.assertEqual(params["contract_year"], 2031)
        with self.assertRaises(ResearchError) as error:
            await self.service._akshare_bars(
                ResearchQuery(source="akshare", asset="option", symbol="SR501C4700")
            )
        self.assertEqual(error.exception.status, 422)
        self.assertEqual(self.service._akshare_call.await_count, 2)

    async def test_routes_require_month_keep_alpaca_default_and_hide_internal_metadata(self):
        service = SimpleNamespace(
            akshare_option_chain=AsyncMock(return_value={"contracts": []}),
            option_chain=AsyncMock(return_value={"contracts": []}),
            akshare_months=AsyncMock(return_value={"months": [], "contracts": ["internal"]}),
        )
        app = FastAPI()
        app.include_router(research_router(lambda: service, lambda: None, None))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for query in (
                "source=akshare",
                "source=akshare&month=bad",
                "source=akshare&month=203011&expiry=2030-10-25",
                "source=invalid&month=203011",
            ):
                response = await client.get("/api/research/options/AU?" + query)
                self.assertEqual(response.status_code, 422)
            service.akshare_option_chain.assert_not_awaited()
            self.assertEqual(
                (
                    await client.get("/api/research/options/au?source=akshare&month=203011")
                ).status_code,
                200,
            )
            service.akshare_option_chain.assert_awaited_once_with("AU", "203011")
            await client.get("/api/research/options/SPY")
            service.option_chain.assert_awaited_once_with("SPY", None)
            self.assertNotIn(
                "contracts", (await client.get("/api/research/option-months/AU")).json()
            )
