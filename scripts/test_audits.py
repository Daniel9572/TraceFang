"""Offline acceptance classifications and resume behavior; no live service required."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from audit_market_data import cached_response, compare_option_contracts
from audit_market_data import inspect_bars as market_bars
from audit_research_data import inspect_bars as research_bars
from audit_research_data import option_evidence, reusable_record


class AuditTests(unittest.TestCase):
    def test_resume_never_reuses_failed_or_changed_responses(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            file = output / "quote.json"
            file.write_text('{"last": 1}')
            record = {
                "file": file.name,
                "url": "http://test/quote",
                "status": 200,
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
            }
            self.assertIsNotNone(cached_response(output, file.name, record["url"], [record]))
            for changed in (
                {**record, "status": 502},
                {**record, "url": "http://other"},
                {**record, "sha256": "changed"},
            ):
                self.assertIsNone(cached_response(output, file.name, record["url"], [changed]))
            for status in (0, 502, 504, 200):
                file.write_text(
                    json.dumps(
                        {"status": status, "payload": [], "path": "/contracts", "query": None}
                    )
                )
                value = reusable_record(file, "/contracts", None)
                self.assertEqual(value is not None, status == 200)
            self.assertIsNone(reusable_record(file, "/other", None))

    def test_structure_preserves_unknown_volume_and_does_not_accept_nan(self):
        bar = dict(
            open_time="2026-10-02T01:00:00Z", open="10", high="12", low="9", close="11", volume=None
        )
        self.assertEqual(market_bars([bar])["result"], "structural_pass")
        self.assertEqual(research_bars([bar]), "structural_pass")
        for invalid in ({**bar, "close": "NaN"}, {**bar, "volume": -1}, {**bar, "low": None}):
            self.assertEqual(market_bars([invalid])["result"], "invalid")
            self.assertEqual(research_bars([invalid]), "invalid")
        self.assertEqual(market_bars([])["result"], "empty")
        self.assertEqual(research_bars([bar, bar]), "invalid")
        for signed in ({**bar,"open":"-1690","high":"-1680","low":"-1700","close":"-1695"},
                       {**bar,"open":"0","high":"1","low":"-1","close":"0","volume":"0"}):
            self.assertEqual(market_bars([signed])["result"],"structural_pass")
        self.assertEqual(market_bars([{**bar,"open":"-1690","high":"-1700","low":"-1710","close":"-1695"}])["result"],"invalid")

    def test_option_comparison_requires_identical_observation_and_preserves_null(self):
        api = dict(
            contract_id="au2611C680",
            underlying_contract_id="au2611",
            option_type="call",
            strike=680,
            expiry="2026-10-26",
            contract_multiplier=1000,
            observed_at="2026-09-30T07:25:21Z",
            last=216.54,
            bid=73.82,
            ask=None,
            previous_settlement=216.54,
            volume=None,
            open_interest=0,
            open_interest_change=0,
            turnover=0,
        )
        source = dict(
            contractname="au2611C680",
            updatetime="2026-09-30 15:25:21",
            lastprice="216.54",
            bidprice="73.82",
            askprice="",
            presettlementprice="216.54",
            volume="",
            openinterest="0",
            openinterestchg="0",
            turnover="0",
        )
        master = dict(INSTRUMENTID="au2611C680", EXPIREDATE="20261026", TRADEUNIT="1000")

        def result(row):
            return compare_option_contracts([row], [source], [master])[0]

        self.assertEqual(result(api)["result"], "pass")
        self.assertEqual(result({**api, "volume": 0})["errors"], "volume")
        self.assertEqual(
            result({**api, "observed_at": "2026-09-30T07:25:22Z"})["result"],
            "insufficient_evidence",
        )
        rows = option_evidence(
            {"contracts": [{"symbol": "au2611C680", "last": 216.54}]}, "AU", "202611"
        )
        self.assertEqual(rows[0]["result"], "insufficient_evidence")
        self.assertIn("volume", rows[0]["missing_fields"])


if __name__ == "__main__":
    unittest.main()
