from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from tracefang.application.research import (
    ResearchDataService,
    ResearchError,
    ResearchQuery,
    china_history_end,
    normalize_bars,
    period_has_closed,
)
from tracefang.research_api import (
    AnalysisRequest,
    ResearchAnalysisJobs,
    research_router,
    technical_evidence,
)


def wire_rows():
    return [
        {"d": f"2025-01-{day:02}", "o": "100", "h": "110", "l": "95", "c": str(100 + day), "v": "0"}
        for day in range(1, 5)
    ]


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.calls = []
        self.fail = False

        async def handler(request):
            self.calls.append(request)
            if self.fail:
                return httpx.Response(403, json={"message": "secret-provider-message"})
            await asyncio.sleep(0.005)
            return httpx.Response(200, text="var result=(" + json.dumps(wire_rows()) + ");")

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.service = ResearchDataService(
            Path(self.directory.name) / "cache.db", client=self.client, environment={}
        )
        self.query = ResearchQuery(source="sina", symbol="AU0", asset="future", limit=2)

    async def asyncTearDown(self):
        await self.service.close()
        await self.client.aclose()
        self.directory.cleanup()

    async def test_concurrent_pages_share_one_fetch_and_cache_survives_restart(self):
        first, second = await asyncio.gather(
            self.service.bars(self.query), self.service.bars(self.query)
        )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(first["items"], second["items"])
        self.assertEqual([row["close"] for row in first["items"]], [103, 104])
        self.assertIsNotNone(first["next_before"])
        replacement = ResearchDataService(
            self.service.cache_path, client=self.client, environment={}
        )
        try:
            page = await replacement.bars(self.query)
            self.assertEqual(page["cache_state"], "cached")
            self.assertEqual(len(self.calls), 1)
        finally:
            await replacement.close()

    async def test_exclusive_cursor_progress_and_source_isolation(self):
        page = await self.service.bars(self.query)
        older = await self.service.bars(
            self.query.model_copy(update={"before": datetime.fromisoformat(page["next_before"])})
        )
        self.assertEqual([row["close"] for row in older["items"]], [101, 102])
        self.assertIsNone(older["next_before"])
        self.assertLess(older["items"][-1]["open_time"], page["items"][0]["open_time"])
        self.fail = True
        with self.assertRaises(ResearchError):
            await self.service.bars(self.query.model_copy(update={"symbol": "AG0"}))

    async def test_failure_preserves_only_same_query_and_does_not_expose_body(self):
        await self.service.bars(self.query)
        self.fail = True
        page = await self.service.bars(self.query, refresh=True)
        self.assertEqual(page["cache_state"], "stale")
        self.assertNotIn("secret-provider-message", json.dumps(page))
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(self.service.sources()[1]["diagnostic"]["state"], "error")

    async def test_cancelled_waiter_does_not_cancel_shared_fetch(self):
        first = asyncio.create_task(self.service.bars(self.query))
        second = asyncio.create_task(self.service.bars(self.query))
        await asyncio.sleep(0.001)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        result = await second
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(len(self.calls), 1)

    async def test_unwritable_cache_still_returns_real_data_with_warning(self):
        parent = Path(self.directory.name) / "not-a-directory"
        parent.write_text("keep this file")
        self.service.cache_path = parent / "cache.db"
        page = await self.service.bars(self.query)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(any("缓存写入失败" in warning for warning in page["warnings"]))
        self.assertEqual(parent.read_text(), "keep this file")

    async def test_source_capabilities_and_missing_credentials_fail_before_network(self):
        for query in [
            self.query.model_copy(update={"period": "1m"}),
            self.query.model_copy(update={"adjustment": "forward"}),
            ResearchQuery(source="alpaca", symbol="AAPL"),
        ]:
            with self.assertRaises(ResearchError):
                await self.service.bars(query)
        self.assertEqual(self.calls, [])

    def test_normalization_rejects_bad_ohlc_nan_and_retains_missing_volume(self):
        row = {"time": "2025-01-01", "open": 10, "high": 12, "low": 9, "close": 11, "volume": None}
        bars, rejected = normalize_bars(
            [
                row,
                {**row, "time": "2025-01-02", "close": 20},
                {**row, "open": "NaN"},
                {**row, "close": 10},
            ],
            self.query,
        )
        self.assertEqual(rejected, 2)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["close"], 10)
        self.assertIsNone(bars[0]["volume"])
        self.assertEqual(bars[0]["open_time"], "2024-12-31T16:00:00+00:00")

    async def test_http_contract_and_error_status(self):
        app = FastAPI()
        jobs = ResearchAnalysisJobs()
        app.include_router(research_router(lambda: self.service, lambda: None, jobs))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/research/bars", json=self.query.model_dump(mode="json")
            )
            self.assertEqual(response.status_code, 200)
            bad = await client.post(
                "/api/research/bars", json={**self.query.model_dump(), "symbol": "../secret"}
            )
            self.assertEqual(bad.status_code, 422)
            missing = await client.post(
                "/api/research/bars", json={"source": "alpaca", "symbol": "AAPL"}
            )
            self.assertEqual(missing.status_code, 409)

    async def test_alpaca_follows_short_pages_and_keeps_explicit_feed(self):
        requests = []

        def handler(request):
            requests.append(request)
            day = "02" if request.url.params.get("page_token") else "03"
            return httpx.Response(
                200,
                json={
                    "bars": {
                        "SPY": [
                            {
                                "t": f"2025-01-{day}T14:30:00Z",
                                "o": 100,
                                "h": 102,
                                "l": 99,
                                "c": 101,
                                "v": 123,
                            }
                        ]
                    },
                    "next_page_token": "next" if len(requests) == 1 else None,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = ResearchDataService(
                Path(self.directory.name) / "alpaca.db",
                client=client,
                environment={"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"},
            )
            try:
                result = await service.bars(
                    ResearchQuery(source="alpaca", symbol="SPY", asset="etf", limit=2)
                )
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[1].url.params["page_token"], "next")
                self.assertTrue(all(request.url.params["feed"] == "iex" for request in requests))
                self.assertEqual(len(result["items"]), 2)
            finally:
                await service.close()

    async def test_tushare_native_option_api_and_no_secret_in_public_sources(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "fields": ["trade_date", "open", "high", "low", "close", "vol"],
                        "items": [["20250102", 1, 2, 0.5, 1.5, None]],
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = ResearchDataService(
                Path(self.directory.name) / "tushare.db",
                client=client,
                environment={"TUSHARE_TOKEN": "test-secret"},
            )
            try:
                result = await service.bars(
                    ResearchQuery(source="tushare", symbol="10008000.SH", asset="option")
                )
                self.assertEqual(requests[0]["api_name"], "opt_daily")
                self.assertEqual(requests[0]["params"]["ts_code"], "10008000.SH")
                self.assertIsNone(result["items"][0]["volume"])
                self.assertNotIn("test-secret", json.dumps(service.sources()))
            finally:
                await service.close()

    def test_week_and_month_end_labels_use_calendar_boundary(self):
        query = ResearchQuery(source="eastmoney", symbol="510300.SH", period="1M")
        last_session = datetime.fromisoformat("2025-02-28T00:00:00+08:00")
        self.assertFalse(period_has_closed(last_session, query, last_session))
        self.assertTrue(period_has_closed(last_session, query, datetime(2025, 3, 1, tzinfo=UTC)))
        query = query.model_copy(update={"period": "1w"})
        self.assertFalse(period_has_closed(last_session, query, datetime(2025, 3, 1, tzinfo=UTC)))
        self.assertTrue(period_has_closed(last_session, query, datetime(2025, 3, 3, tzinfo=UTC)))

    def test_paging_week_and_month_does_not_repeat_partial_calendar_period(self):
        query = ResearchQuery(
            source="tencent",
            symbol="600519.SH",
            period="1w",
            before=datetime.fromisoformat("2025-02-28T00:00:00+08:00"),
        )
        self.assertEqual(china_history_end(query).isoformat(), "2025-02-23T23:59:59+08:00")
        query = query.model_copy(update={"period": "1M"})
        self.assertEqual(china_history_end(query).isoformat(), "2025-01-31T23:59:59+08:00")

    async def test_tencent_adjustment_is_never_silently_replaced_with_raw(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "sh510300": {"day": [["2025-01-02", "4", "4.1", "4.2", "3.9", "100"]]}
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = ResearchDataService(Path(self.directory.name) / "tencent.db", client=client)
            try:
                query = ResearchQuery(source="tencent", symbol="510300.SH", asset="etf", limit=2)
                result = await service.bars(query)
                self.assertEqual(result["items"][0]["close"], 4.1)
                self.assertIn("sh510300,day,", requests[0].url.params["param"])
                with self.assertRaisesRegex(ResearchError, "复权"):
                    await service.bars(query.model_copy(update={"adjustment": "forward"}))
            finally:
                await service.close()

    async def test_option_chain_pagination_occ_and_missing_quote(self):
        requests = []

        def handler(request):
            requests.append(request)
            code = (
                "SPY270115P00500000" if "page_token" in request.url.params else "SPY270115C00500000"
            )
            return httpx.Response(
                200,
                json={
                    "snapshots": {
                        code: {
                            "latestTrade": {"p": 4, "t": "2026-09-24T14:30:00Z"},
                            "impliedVolatility": 0.2,
                        }
                    },
                    "next_page_token": "page2" if len(requests) == 1 else None,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = ResearchDataService(
                Path(self.directory.name) / "chain.db",
                client=client,
                environment={"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"},
            )
            try:
                result = await service.option_chain("SPY", "2027-01-15")
                self.assertEqual(len(result["contracts"]), 2)
                self.assertFalse(result["truncated"])
                self.assertEqual(result["contracts"][0]["strike"], 500)
                self.assertIsNone(result["contracts"][0]["bid"])
                self.assertTrue(all(r.url.params["feed"] == "indicative" for r in requests))
                self.assertTrue(
                    all(r.url.params["expiration_date"] == "2027-01-15" for r in requests)
                )
            finally:
                await service.close()

    async def test_analysis_cancellation_and_stale_data_gate(self):
        jobs = ResearchAnalysisJobs()
        ai = SimpleNamespace(analyze=AsyncMock(side_effect=lambda *_args, **_kwargs: None))
        await self.service.bars(self.query)
        page = await self.service.bars(self.query)
        stale = SimpleNamespace(bars=AsyncMock(return_value={**page, "cache_state": "stale"}))
        created = jobs.start(AnalysisRequest(query=self.query), stale, ai)
        await asyncio.gather(*list(jobs.tasks.values()))
        self.assertEqual(jobs.get(created["id"])["state"], "failed")
        ai.analyze.assert_not_called()

        async def wait(_):
            await asyncio.Event().wait()

        waiting = SimpleNamespace(bars=wait)
        created = jobs.start(AnalysisRequest(query=self.query), waiting, ai)
        self.assertEqual((await jobs.cancel(created["id"]))["state"], "cancelled")
        self.assertFalse(jobs.tasks)
        await jobs.close()

    def test_computed_evidence_excludes_unclosed_bar(self):
        bars = [
            {"state": "final", "close": float(i), "high": i + 1, "low": i - 1} for i in range(1, 22)
        ]
        evidence = technical_evidence([*bars, {"state": "provisional_authoritative", "close": 900}])
        self.assertEqual(evidence["sma_20"], 11.5)
        self.assertEqual(evidence["closed_bars"], 21)
        self.assertEqual(evidence["excluded_open_bars"], 1)
        self.assertIsNone(evidence["sma_60"])


if __name__ == "__main__":
    unittest.main()
