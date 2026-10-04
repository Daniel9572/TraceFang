from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from tracefang.akshare_worker import AK_UNDERLYINGS, build_chain, etf_underlying_symbol, quote_time, normalize_metadata
from tracefang.sina_option_exact import exact_decimal, json_exact, etf_quote, table_quotes, fetch_raw


def reply(text):
    body=text.encode()
    return text,{"url":"https://test.invalid/source","received_at":"2030-09-30T08:00:00+00:00","body_base64":base64.b64encode(body).decode(),"body_sha256":hashlib.sha256(body).hexdigest(),"encoding":"utf-8","byte_count":len(body)}


def etf_fields(code, *, strike="2.8000", bid="0", ask="0.1410000000000000000000000001", last="0.1400", underlying="510050"):
    fields=["0"]*51
    for index,value in {1:bid,2:last,3:ask,5:"0",7:strike,8:"0.1300",32:"2030-09-30 16:00:00",36:underlying,41:"0"}.items():fields[index]=value
    return f'var hq_str_CON_OP_{code}="'+','.join(fields)+'";'


class SinaOptionExactTests(unittest.TestCase):
    def test_research_config_and_worker_use_the_same_proven_product_and_etf_identities(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "backend/src/research/config.json").read_text())
        worker = {row["symbol"]: row for row in AK_UNDERLYINGS}
        self.assertEqual(len(worker), len(AK_UNDERLYINGS))
        self.assertEqual(worker, {row["symbol"]: row for row in config["underlyings"]})
        self.assertEqual(worker["A"]["sina"], "黄大豆1号期权")
        self.assertEqual(worker["B"]["sina"], "黄大豆2号期权")
        self.assertEqual(worker["PX"]["sina"], "二甲苯期权")
        for symbol in ["A", "B", "Y", "EG", "EB", "SI", "LC", "PX", "SH", "ZC"]:
            self.assertEqual(worker[symbol]["category"], "future")
        for symbol in ["159901.SZ", "159915.SZ", "159919.SZ", "159922.SZ", "510500.SH", "588000.SH", "588080.SH"]:
            self.assertEqual(worker[symbol]["category"], "etf")

    def test_etf_reference_requests_use_the_exact_market_suffix_and_reject_unknown_suffixes(self):
        for spec in (row for row in AK_UNDERLYINGS if row["category"] == "etf"):
            symbol = spec["symbol"]
            code = "90000001" if symbol.endswith(".SZ") else "10000001"
            metadata = [{"symbol":code+symbol[-3:], "underlying":symbol, "expiry":"2030-10-28", "month":"203010", "kind":"call", "strike":"2.8", "multiplier":"10000", "currency":"CNY"}]
            prefix = symbol[-2:].lower() + symbol[:6]
            calls = []
            def source(url, params=None):
                calls.append(url)
                name = url.split("list=")[1]
                if name.startswith("OP_"):
                    return reply(f'var hq_str_{name}="CON_OP_{code}";')
                if name.startswith("CON_OP_"):
                    return reply(etf_fields(code, underlying=symbol[:6]))
                self.assertEqual(name, prefix)
                fields = ["0"]*32
                fields[3], fields[30], fields[31] = "9007199254740993.0000000000000000000000000001", "2030-09-30", "15:34:59"
                return reply(f'var hq_str_{name}="' + ','.join(fields) + '";')
            with self.subTest(symbol=symbol), patch('tracefang.akshare_worker.fetch_raw', side_effect=source):
                chain = build_chain(None, {"symbol":symbol, "month":"203010", "contracts":metadata})
                self.assertIn("https://hq.sinajs.cn/list="+prefix, calls)
                self.assertEqual(chain["reference_spot"], "9007199254740993.0000000000000000000000000001")
                self.assertEqual(chain["contracts"][0]["underlying"], symbol)
                self.assertIsNone(chain["contracts"][0]["observed_at"])
                self.assertEqual(chain["contracts"][0]["observed_precision"],"unknown")
                self.assertEqual(chain["contracts"][0]["source_clock_label"],"2030-09-30 16:00:00")
        for symbol in ["159901", "159901.HK", "159901.sz", "510500.NY"]:
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                etf_underlying_symbol(symbol)

    def test_zero_last_and_midnight_etf_label_preserve_raw_without_claiming_a_quote_instant(self):
        metadata = [{"symbol":"90008127.SZ", "underlying":"159901.SZ", "expiry":"2030-10-28", "month":"203010", "kind":"call", "strike":"2.95", "multiplier":"10000", "currency":"CNY"}]
        text = etf_fields("90008127", strike="2.9500", bid="0.3391", ask="0.3530", last="0.0000", underlying="159901").replace("2030-09-30 16:00:00", "2030-09-30 00:00:00")
        def source(url, params=None):
            name = url.split("list=")[1]
            if name.startswith("OP_"): return reply(f'var hq_str_{name}="CON_OP_90008127";')
            if name.startswith("CON_OP_"): return reply(text)
            return reply('var hq_str_sz159901="'+','.join(['0']*32)+'";')
        with patch('tracefang.akshare_worker.fetch_raw', side_effect=source):
            chain = build_chain(None, {"symbol":"159901.SZ", "month":"203010", "contracts":metadata})
        row = chain["contracts"][0]
        self.assertIsNone(row["last"])
        self.assertIsNone(row["observed_at"])
        self.assertEqual(row["observed_precision"], "unknown")
        self.assertEqual(row["source_quote"]["observed_label"], "2030-09-30 00:00:00")
        self.assertEqual(row["source_quote"]["raw_fields"]["fields"][2], "0.0000")
        self.assertEqual((row["bid"], row["ask"]), ("0.3391", "0.3530"))
        self.assertEqual((row["volume"], row["open_interest"]), ("0", "0"))
        self.assertIsNone(chain["reference_spot"])
        self.assertEqual(chain["reference_precision"], "unknown")
        self.assertTrue(any("午夜" in warning for warning in chain["warnings"]))

    def test_json_and_source_decimals_keep_original_precision_and_reject_float_inputs(self):
        raw='{"price":9007199254740993.0000000000000000000000000001,"tiny":0.0000000000000000000000000001,"count":18446744073709551615}'
        value=json_exact(raw)
        self.assertEqual(value["price"],"9007199254740993.0000000000000000000000000001")
        self.assertEqual(value["count"],"18446744073709551615")
        self.assertEqual(exact_decimal(value["tiny"]),"0.0000000000000000000000000001")
        self.assertIsNone(exact_decimal(0.141))
        self.assertEqual(exact_decimal("0"),"0")
        self.assertIsNone(exact_decimal("0",positive=True))
        self.assertIsNone(exact_decimal("NaN"))

    def test_table_fields_quantities_zero_missing_and_unknown_clock(self):
        text='{"result":{"data":{"up":[["1","9007199254740993.0000000000000000000000000001","30.5000","9007199254740994","2","0","-0.0001","900","m3011-C-900"]],"down":[["0","-","-","-","0","-","-","m3011-P-900"]]}}}'
        rows=table_quotes(text)
        self.assertEqual(rows[0]["bid_raw"],"9007199254740993.0000000000000000000000000001")
        self.assertEqual(rows[0]["open_interest_raw"],"0")
        self.assertIsNone(rows[0]["observed_at_raw"])
        metadata=normalize_metadata([{"交易所ID":"DCE","合约ID":"m3011-C-900","标的合约ID":"m3011","最后交易日":"20301025","期权类型":"1","行权价":"900.00","合约乘数":"10","交割年份":"2030","交割月份":"11"}])["contracts"]
        def source(url,params=None):
            if 'optionsDP.php' in url:return reply('<a href="/futures/view/optionsDP.php/m_o/dce">豆粕期权</a>')
            if 'getDailyKLine' in url:return reply('var daily=([{"date":"2030-09-30","close":"950.0000000000000000000000000001"}]);')
            return reply(text)
        with patch('tracefang.akshare_worker.fetch_raw',side_effect=source):chain=build_chain(None,{"symbol":"M","month":"203011","contracts":metadata})
        contract=chain["contracts"][0]
        self.assertEqual(contract["bid"],rows[0]["bid_raw"])
        self.assertEqual(contract["open_interest"],"0")
        self.assertIsNone(contract["volume"])
        self.assertEqual(contract["source_change"],"-0.0001")
        self.assertEqual(contract["source_change_unit"],"unknown")
        self.assertEqual(table_quotes('{"result":{"status":{"code":0},"data":{"info":[]}}}'),[])
        self.assertIsNone(contract["observed_at"])
        self.assertIsNone(chain["reference_observed_at"])
        self.assertEqual(chain["reference_date"],"2030-09-30")
        self.assertEqual(chain["reference_precision"],"day")
        self.assertEqual(chain["reference_spot"],"950.0000000000000000000000000001")
        self.assertIsNone(quote_time("2030-09-30"))

    def test_etf_more_than_160_codes_preserves_every_metadata_contract_and_source_lexeme(self):
        codes=[str(10000000+i) for i in range(172)]
        metadata=[{"symbol":code+".SH","underlying":"510050.SH","expiry":"2030-10-28","month":"203011","kind":"call","strike":"2.8","multiplier":"10000","currency":"CNY"} for code in codes]
        def source(url,params=None):
            if 'OP_UP_' in url or 'OP_DOWN_' in url:
                name=url.split('list=')[1]
                return reply('var hq_str_'+name+'="'+','.join('CON_OP_'+code for code in codes)+'";')
            if 'CON_OP_' in url:return reply(etf_fields(url.split('CON_OP_')[1]))
            return reply('var hq_str_sh510050="'+','.join(['0']*32)+'";')
        with patch('tracefang.akshare_worker.fetch_raw',side_effect=source):chain=build_chain(None,{"symbol":"510050.SH","month":"203011","contracts":metadata})
        self.assertFalse(chain["truncated"])
        self.assertEqual(len(chain["contracts"]),172)
        self.assertEqual(chain["quoted_contract_count"],"172")
        row=chain["contracts"][0]
        self.assertEqual(row["ask"],"0.1410000000000000000000000001")
        self.assertIsNone(row["bid"])
        self.assertEqual(row["source_quote"]["raw_fields"]["fields"][1],"0")
        self.assertEqual(row["open_interest"],"0")
        self.assertEqual(row["volume"],"0")
        self.assertIsNone(row["observed_at"])
        self.assertEqual(row["source_clock_qualification"],"unverified_timezone_and_role")
        for proof in chain["source_evidence"]:
            self.assertEqual(hashlib.sha256(base64.b64decode(proof["body_base64"])).hexdigest(),proof["body_sha256"])

    def test_actual_daily_reference_fields_and_missing_date_do_not_invent_day_precision(self):
        metadata=[{"symbol":"M3011C900","underlying":"M3011","expiry":"2030-10-28","month":"203011","kind":"call","strike":"900","multiplier":"10","currency":"CNY"}]
        for daily, expected in [
            ({"d":"2030-09-30","c":"950.0000000000000000000000000001"}, "950.0000000000000000000000000001"),
            ({"c":"950"}, None),
            ({"d":None,"c":"950"}, None),
            ({"d":"None","c":"950"}, None),
            ({"d":"2030-02-30","c":"950"}, None),
            ({"d":"2030-09-30","c":"0"}, None),
        ]:
            def source(url, params=None):
                if 'optionsDP.php' in url:return reply('<a href="/futures/view/optionsDP.php/m_o/dce">豆粕期权</a>')
                if 'getDailyKLine' in url:return reply('var daily=('+json.dumps([daily])+');')
                return reply('{"result":{"status":{"code":0},"data":{"info":[]}}}')
            with self.subTest(daily=daily),patch('tracefang.akshare_worker.fetch_raw',side_effect=source):
                chain=build_chain(None,{"symbol":"M","month":"203011","contracts":metadata})
                self.assertEqual(chain["reference_spot"], expected)
                self.assertEqual(chain["reference_date"], "2030-09-30" if expected else None)
                self.assertEqual(chain["reference_precision"], "day" if expected else "unknown")
                self.assertIsNone(chain["reference_observed_at"])

    def test_body_bound_is_checked_before_accumulating_unbounded_source_bytes(self):
        class Response:
            headers={"content-length":"9"};url='https://test.invalid/'
            def raise_for_status(self):pass
            def iter_bytes(self):yield b'12345';yield b'67890'
            def __enter__(self):return self
            def __exit__(self,*args):pass
        with patch('tracefang.sina_option_exact.MAX_BODY',8),patch('tracefang.sina_option_exact.httpx.stream',return_value=Response()):
            with self.assertRaises(ValueError):fetch_raw('https://test.invalid/')
        with self.assertRaises(ValueError):etf_quote('var hq_str_CON_OP_10000000="0";','10000000')
