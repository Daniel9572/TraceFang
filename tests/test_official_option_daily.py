import asyncio
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import AsyncMock, patch

import httpx

from tracefang.akshare_worker import AK_UNDERLYINGS, build_chain
from tracefang.application.research import ResearchDataService, ResearchError
from tracefang.official_option_daily import LABELS, build_daily_chain, fetch_report, packet_body, parse_report


def czce_body(close="0", stamp="2026-09-30", duplicate=False):
    fields = ["AP611C9000", "12.0000", "1", "12", "0", close, "12.0001", "-0.0001", "0.0001", "0", "0", "0", "0", "0.2", "3", "0"]
    line = " | ".join(fields)
    return (f"郑州商品交易所期权每日行情表({stamp})\n" + " | ".join(LABELS) + "\n" + line + ("\n" + line if duplicate else "")).encode()


def proof_packet(raw, family="czce-option-daily", stamp="2026-09-30"):
    return {"source_family": family, "source_date": stamp, "precision_policy": "source-decimal-lexeme-v1", "source_evidence": {"status": 200, "byte_count": len(raw), "body_sha256": hashlib.sha256(raw).hexdigest(), "body_base64": base64.b64encode(raw).decode(), "received_at": "2026-10-04T04:00:00+00:00"}}


def meta(symbol="AP611C9000", underlying="AP611", exchange="CZCE"):
    return {"symbol": symbol, "underlying": underlying, "month": "202611", "expiry": "2026-10-27", "exchange": exchange, "kind": "call", "strike": "9000", "multiplier": "10", "currency": "CNY"}


class OfficialDailyTests(unittest.TestCase):
    def test_config_total_and_sources_are_explicit(self):
        config = json.loads((Path(__file__).parents[1] / "backend/src/research/config.json").read_text())
        self.assertEqual({row["symbol"]:row for row in config["underlyings"]}, {row["symbol"]:row for row in AK_UNDERLYINGS})
        self.assertEqual(len(AK_UNDERLYINGS), 60)
        self.assertEqual(sum(row.get("quote_source") in {"czce-option-daily", "gfex-option-daily"} for row in AK_UNDERLYINGS), 13)
        self.assertEqual(sum(row.get("quote_source") == "catalog-only" for row in AK_UNDERLYINGS), 11)
        # Research and SHFE overlap AU/CU/RU. Directory count is not price coverage.
        self.assertEqual(60 + 23 - 3, 80)

    def test_zero_close_preserved_without_settlement_or_clock_fallback(self):
        params = {"symbol": "AP", "month": "202611", "contracts": [meta()], "report_date": "2026-09-30", "daily_source_packet": proof_packet(czce_body())}
        with patch("tracefang.official_option_daily.fetch_report", side_effect=AssertionError("unexpected network")):
            result = build_chain(None, params)
        row = result["contracts"][0]
        self.assertEqual((row["daily_close"], row["settlement"], row["volume"]), ("0", "12.0001", "0"))
        self.assertIsNone(row["last"])
        self.assertIsNone(row["bid"])
        self.assertIsNone(row["observed_at"])
        self.assertEqual(row["observed_precision"], "day")
        self.assertEqual(row["source_date"], "2026-09-30")
        self.assertEqual(row["source_received_at"], "2026-10-04T04:00:00+00:00")
        self.assertEqual(result["reference_precision"], "unknown")

    def test_wide_source_prices_and_original_lexemes_survive(self):
        value = "9007199254740993.0000000000000000000000000001"
        result = build_daily_chain({"symbol": "AP", "month": "202611", "contracts": [meta()], "daily_source_packet": proof_packet(czce_body(value))}, next(row for row in AK_UNDERLYINGS if row["symbol"] == "AP"))
        row = result["contracts"][0]
        self.assertEqual(row["last"], value)
        self.assertEqual(row["source_quote"]["raw_fields"]["今收盘"], value)
        self.assertEqual(row["source_change"], "-0.0001")

    def test_requested_date_mismatch_duplicate_integrity_and_identity_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "日期"):
            parse_report("czce-option-daily", czce_body(), "2026-09-29")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_report("czce-option-daily", czce_body(duplicate=True), "2026-09-30")
        packet = proof_packet(czce_body())
        packet["source_evidence"]["body_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "integrity"):
            packet_body(packet, "czce-option-daily", "2026-09-30")
        record = meta(); record["strike"] = "9001"
        result = build_chain(None, {"symbol": "AP", "month": "202611", "contracts": [record], "daily_source_packet": proof_packet(czce_body("12"))})
        self.assertIsNone(result["contracts"][0]["last"])
        self.assertNotIn("source_quote", result["contracts"][0])

    def test_gfex_date_echo_required_and_zero_volume_does_not_invent_trade(self):
        fields = {"delivMonth": "ps2611-C-9000", "varietyOrder": "ps", "variety": "多晶硅", "close": "1.0000000000000000000000000001", "clearPrice": "2", "lastClear": "3", "open": "1", "high": "2", "low": "1", "diff": "-2", "diff1": "-1", "volumn": "0", "openInterest": "0", "turnover": "0"}
        packet = {"code": 0, "param": {"trade_date": ["20260930"], "trade_type": ["1"]}, "data": [fields], "time": "not a quote time"}
        raw = json.dumps(packet).encode()
        result = build_chain(None, {"symbol": "PS", "month": "202611", "contracts": [meta("PS2611C9000", "PS2611", "GFEX")], "daily_source_packet": proof_packet(raw, "gfex-option-daily")})
        row = result["contracts"][0]
        self.assertEqual(row["last"], fields["close"])
        self.assertEqual(row["volume"], "0")
        self.assertIsNone(row["quantity_units"]["volume"])
        self.assertIsNone(row["observed_at"])
        self.assertIsNone(row["iv"])
        packet["param"]["trade_date"] = ["20260929"]
        with self.assertRaisesRegex(ValueError, "日期"):
            parse_report("gfex-option-daily", json.dumps(packet).encode(), "2026-09-30")

    def test_catalog_only_never_fetches_or_claims_other_dce_requests(self):
        for spec in (row for row in AK_UNDERLYINGS if row.get("quote_source") == "catalog-only"):
            row = meta(spec["symbol"] + "2611C9000", spec["symbol"] + "2611", "DCE")
            with patch("tracefang.akshare_worker.fetch_raw", side_effect=AssertionError("unexpected network")):
                result = build_chain(None, {"symbol": spec["symbol"], "month": "202611", "contracts": [row]})
            self.assertEqual(result["quoted_contract_count"], "0")
            self.assertEqual(result["source_availability"]["attempted_product_request"], spec["symbol"] == "PP")
            self.assertIsNone(result["contracts"][0]["last"])

    def test_bounded_fetch_keeps_actual_get_post_receipt_and_rejects_other_dates(self):
        for family, raw in [("czce-option-daily", czce_body("12"))]:
            response = httpx.Response(200, content=raw, request=httpx.Request("GET", "https://www.czce.com.cn/report"))
            with patch("tracefang.official_option_daily.httpx.stream", return_value=nullcontext(response)) as stream:
                result = fetch_report(family, "2026-09-30")
            self.assertEqual(stream.call_args.args[0], "GET")
            self.assertEqual(result["source_evidence"]["body_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(packet_body(result, family, "2026-09-30")[0], raw)
            with patch("tracefang.official_option_daily.httpx.stream", return_value=nullcontext(httpx.Response(200, content=raw, request=httpx.Request("GET", "https://www.czce.com.cn/report")))), self.assertRaisesRegex(ValueError, "日期"):
                fetch_report(family, "2026-10-01")
        fields = {"delivMonth":"ps2611-C-9000","varietyOrder":"ps","close":"1","clearPrice":"2","lastClear":"3","open":"1","high":"2","low":"1","diff":"-2","diff1":"-1","volumn":"0","openInterest":"0","turnover":"0"}
        raw = json.dumps({"code":0,"param":{"trade_date":["20260930"],"trade_type":["1"]},"data":[fields]}).encode()
        response = httpx.Response(200,content=raw,request=httpx.Request("POST","http://www.gfex.com.cn/report"))
        with patch("tracefang.official_option_daily.httpx.stream",return_value=nullcontext(response)) as stream:
            result = fetch_report("gfex-option-daily","2026-09-30")
        self.assertEqual(stream.call_args.args[0],"POST")
        self.assertEqual(stream.call_args.kwargs["data"],{"trade_date":"20260930","trade_type":"1"})
        self.assertEqual(result["source_evidence"]["method"],"POST")
        response = httpx.Response(200,content=raw,headers={"content-length":str(4*1024*1024+1)},request=httpx.Request("POST","http://www.gfex.com.cn/report"))
        with patch("tracefang.official_option_daily.httpx.stream",return_value=nullcontext(response)), self.assertRaisesRegex(ValueError,"bound"):
            fetch_report("gfex-option-daily","2026-09-30")


class DailyResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_report_date_can_refresh_revision_without_relabeling_old_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ResearchDataService(Path(directory) / "research.db", environment={})
            versions = [proof_packet(czce_body("12")), proof_packet(czce_body("13"))]
            versions[1]["source_evidence"]["received_at"] = "2026-10-04T04:06:00+00:00"
            source_calls = 0
            async def worker(operation, params):
                nonlocal source_calls
                if operation == "metadata": return {"contracts":[meta()]}
                if operation == "daily_source":
                    version = versions[min(source_calls,1)]
                    source_calls += 1
                    return version
                return build_chain(None, params)
            service._akshare_call = AsyncMock(side_effect=worker)
            with patch("tracefang.application.research.time.time",return_value=1000):
                first = await service.akshare_option_chain("AP","202611")
            with patch("tracefang.application.research.time.time",return_value=1299):
                cached = await service.akshare_option_chain("AP","202611")
            self.assertEqual(source_calls,1)
            self.assertEqual(cached["contracts"][0]["daily_close"],"12")
            self.assertEqual(cached["fetched_at"],first["fetched_at"])
            with patch("tracefang.application.research.time.time",return_value=1301):
                revised = await service.akshare_option_chain("AP","202611")
            self.assertEqual(source_calls,2)
            self.assertEqual(revised["contracts"][0]["daily_close"],"13")
            self.assertEqual(revised["source_date"],first["source_date"])
            self.assertNotEqual(revised["source_evidence"][0]["body_sha256"],first["source_evidence"][0]["body_sha256"])
            self.assertEqual(first["fetched_at"],"2026-10-04T04:00:00+00:00")
            self.assertEqual(revised["fetched_at"],"2026-10-04T04:06:00+00:00")
            await service.close()

    async def test_two_products_and_months_share_one_source_report_body(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ResearchDataService(Path(directory) / "research.db", environment={})
            await self._exercise(service)
            await service.close()

    async def _exercise(self, service):
        records = [meta(), {**meta("CJ611C9000", "CJ611"), "month": "202611"}]
        source = proof_packet(czce_body("12"))
        async def worker(operation, params):
            if operation == "metadata":
                return {"contracts": records}
            if operation == "daily_source":
                return source
            return {"contracts": [meta()], "warnings": [], "fetched_at": "2026-10-04T04:00:00+00:00"}
        service._akshare_call = AsyncMock(side_effect=worker)
        first, second = await asyncio.gather(service.akshare_option_chain("AP", "202611"), service.akshare_option_chain("CJ", "202611"))
        calls = [call.args[0] for call in service._akshare_call.call_args_list]
        self.assertEqual(calls.count("daily_source"), 1)
        self.assertEqual(first["fetched_at"], "2026-10-04T04:00:00+00:00")
        with self.assertRaises(ResearchError):
            await service.akshare_option_chain("AP", "202611", "2026-9-30")


if __name__ == "__main__":
    unittest.main()
