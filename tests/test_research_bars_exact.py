from __future__ import annotations
import json
import unittest
from unittest.mock import patch
from tracefang.akshare_worker import load_bars
from tracefang.research_bars_exact import jsonp
from tests.test_sina_option_exact import reply

class ResearchBarsExactTests(unittest.TestCase):
    def query(self,asset,period,symbol):return {'asset':asset,'period':period,'symbol':symbol,'adjustment':'raw','end_date':'20300930'}
    def test_all_13_configured_combinations_keep_source_decimal_strings_and_clock_evidence(self):
        for asset,periods,symbol in [('future',['1m','5m','15m','30m','1h','1d'],'AU0'),('option',['1d'],'AU3011C900'),('equity',['1d','1w','1M'],'600519.SH'),('etf',['1d','1w','1M'],'510050.SH')]:
            for period in periods:
                is_em=asset in {'equity','etf'};minute=asset=='future' and period!='1d';label='2030-09-30 15:00:00' if minute else '2030-09-30';wide='9007199254740993.0000000000000000000000000001'
                packet={'rc':0,'data':{'code':symbol[:6],'klines':[label+','+','.join([wide,wide,wide,wide,'0','0','0','0','0','0'])]}} if is_em else [{'d':label,'o':wide,'h':wide,'l':wide,'c':wide,'v':'0','p':None}]
                raw=json.dumps(packet) if is_em else 'var bars=('+json.dumps(packet)+');'
                with self.subTest(asset=asset,period=period),patch('tracefang.research_bars_exact.fetch_raw',return_value=reply(raw)):
                    output=load_bars(object(),self.query(asset,period,symbol))
                    self.assertEqual(output['bars'][0]['close'],wide)
                    self.assertEqual(output['bars'][0]['volume'],'0')
                    self.assertIsNone(output['bars'][0]['open_interest'])
                    self.assertEqual(output['bars'][0]['source_payload']['source_label'],label)
                    self.assertFalse(output['bars'][0]['source_payload']['clock_policy_verified'])
                    self.assertEqual(len(output['source_evidence']),1)
                    self.assertEqual(output['source_evidence'][0]['body_base64'],reply(raw)[1]['body_base64'])
    def test_etf_and_three_cffex_option_protocols_keep_cp_identity_and_unknown_fields(self):
        for symbol in ['10000000.SH','IO3011C3000','HO3011P2500','MO3011C6000','SR101C4700']:
            query=self.query('option','1d',symbol);query['contract_year']=2030
            with self.subTest(symbol=symbol),patch('tracefang.research_bars_exact.fetch_raw',return_value=reply('([{ "d":"2030-09-30","o":-1.0000000000000000000000000001,"h":0,"l":-2,"c":0,"v":null}]);')) as fetch:
                result=load_bars(None,query)
                self.assertEqual(result['bars'][0]['open'],'-1.0000000000000000000000000001')
                self.assertIsNone(result['bars'][0]['volume'])
                provider=fetch.call_args.args[1]['symbol']
                self.assertEqual(provider,'CON_OP_10000000' if symbol.startswith('1') else 'sr3101c4700' if symbol.startswith('SR') else symbol[:2].lower()+symbol[2:])
    def test_empty_and_invalid_first_row_keep_one_complete_source_envelope(self):
        for raw in ['var bars=(null);','var bars=([]);','var bars=([{ "d":"2030-09-30","o":"-","h":"2","l":"0","c":"1","v":null}]);']:
            with self.subTest(raw=raw),patch('tracefang.research_bars_exact.fetch_raw',return_value=reply(raw)):
                output=load_bars(None,self.query('future','1d','AU0'))
                self.assertEqual(len(output['source_evidence']),1)
                if output['bars']:self.assertIsNone(output['bars'][0]['open'])
                else:self.assertIn('retention_floor_not_proven',output['source_response_state'])
        with self.assertRaises((ValueError,json.JSONDecodeError)):jsonp('var bars=(__import__("os").system("false"));')
    def test_unexpected_or_mismatched_source_identity_rejects_without_float_fallback(self):
        for raw in ['{"rc":1,"data":null}','{"rc":0,"data":{"code":"510300","klines":[]}}','{"rc":0,"data":{"code":"510050","klines":["bad"]}}']:
            with self.subTest(raw=raw),patch('tracefang.research_bars_exact.fetch_raw',return_value=reply(raw)),self.assertRaises(ValueError):load_bars(None,self.query('etf','1d','510050.SH'))
