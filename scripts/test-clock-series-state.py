#!/usr/bin/env python3
import copy
import runpy
from pathlib import Path
import unittest

core = runpy.run_path(str(Path(__file__).with_name('prepare-clock-series-state.py')))


class AuthorityState(unittest.TestCase):
    def row(self):
        return dict(source_id='tonghuashun_futures', instrument_symbol='AU8888',
                    interval_seconds=60, provider_symbol='qh_au8888',
                    open_time='2026-09-30T06:58:00Z', close_time='2026-09-30T06:59:00Z',
                    received_at='2026-10-02T11:18:00.123456789Z',
                    finalized_at='2026-09-30T07:00:00.000000001Z', state='final',
                    _legacy_clock_projection=dict(policy_id=core['POLICY'],
                        provider_code='qh_au8888', original_source_record={'table':'candles'}))

    def group(self):
        return dict(authority_rows=0, states={}, confirmed_authority_rows=0, final_clock_unknown_rows=0)

    def test_candles_and_realtime_source_aliases_have_same_exact_boundary(self):
        candle = self.row(); realtime = copy.deepcopy(candle)
        realtime['realtime_source_id'] = realtime.pop('source_id')
        a, b = self.group(), self.group()
        core['derive'](candle, {'row':'1'}, a); core['derive'](realtime, {'row':'1'}, b)
        self.assertEqual(a, b)
        self.assertEqual(a['close_ns'], core['ns']('2026-09-30T06:59:00Z'))
        self.assertEqual(a['received_ns'], core['ns']('2026-10-02T11:18:00.123456789Z'))

    def test_future_preview_quote_and_unproved_final_never_advance_authority(self):
        group = self.group(); core['derive'](self.row(), {}, group)
        at = group['close_ns']
        for kind in ['preview', 'quote', 'unknown_finality']:
            row = self.row(); row['open_time']='2026-09-30T07:00:00Z'; row['close_time']='2026-09-30T07:01:00Z'
            if kind == 'preview': row['state']='provisional_authoritative'
            elif kind == 'quote': row.pop('_legacy_clock_projection')
            else: row['finalized_at']=None
            core['derive'](row, {}, group)
            self.assertEqual(group['close_ns'], at)

    def test_wrong_lineage_or_early_confirmation_is_rejected(self):
        row = self.row(); row['_legacy_clock_projection']['provider_code']='qh_ag8888'
        with self.assertRaises(ValueError): core['derive'](row, {}, self.group())
        row = self.row(); row['finalized_at']='2026-09-30T06:58:59.999999999Z'
        with self.assertRaises(ValueError): core['derive'](row, {}, self.group())


if __name__ == '__main__': unittest.main()
