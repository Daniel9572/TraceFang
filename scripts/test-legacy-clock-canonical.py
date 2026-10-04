#!/usr/bin/env python3
"""Deterministic offline planner tests; no production files or services."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import importlib.util

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('clock_plan', ROOT/'prepare-legacy-clock-canonical.py')
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)


class ClockPlan(unittest.TestCase):
    def test_nanoseconds_are_exact_even_above_float_safe_range(self):
        self.assertEqual(clock.ns('2026-09-30T06:07:01.123456789Z'), 1790748421123456789)
        self.assertEqual(clock.ns('2026-09-30T14:07:01.123456789+08:00'), 1790748421123456789)
        self.assertEqual(clock.ns('1969-12-31T23:59:59.999999999Z'), -1)
        with self.assertRaises(ValueError):
            clock.ns('2026-09-30T06:07:01.1234567891Z')

    def fixture(self, root, mixed=False, early=False):
        source = root/'source'; source.mkdir()
        base = dict(instrument_symbol='AU8888', provider_symbol='qh_au8888', source_id='tonghuashun_futures',
                interval_seconds=60, open_time='2026-09-30T06:07:00Z', observed_at='2026-09-30T06:07:00Z',
                received_at='2026-09-30T06:07:01.123456789Z', open='0.0000000000000000000000000001',
                high='2', low='0.0000000000000000000000000001', close='1', volume=None,
                raw_payload=dict(channel='tonghuashun_public_line_v6', history_file='tonghuashun_public_line_61_year', response_kind='minute_year'))
        point = copy.deepcopy(base); point['open_time'] = '2026-09-30T01:00:00Z'
        for key in ['open', 'high', 'low', 'close']: point[key] = '1'
        restored = copy.deepcopy(base); restored['open_time'] = '2026-09-30T06:10:00Z'
        restored['received_at'] = restored['finalized_at'] = '2026-09-30T06:10:00.000000001Z'; restored['state'] = 'final'
        if early: restored['received_at'] = restored['finalized_at'] = '2026-09-30T06:09:30.000000001Z'
        quote = copy.deepcopy(base); quote['open_time'] = '2026-09-30T06:06:00Z'; quote['state'] = 'provisional_quote'
        quote['raw_payload'] = {'derivation': 'quote_event'}
        originals = {'candles': [base, point, restored], 'realtime_bars': [quote, restored]}
        tables = []
        for name, rows in originals.items():
            path = source/(name+'.ndjson'); path.write_bytes(b''.join(clock.encoded(v) for v in rows))
            tables.append(dict(table=name, file=path.name, sha256=clock.sha(path), rows=str(len(rows))))
        by_table = {v['table']:v for v in tables}
        def ref(name, row):
            return {'table':name, 'file':by_table[name]['file'], 'sha256':by_table[name]['sha256'], 'row_offset':str(row)}
        def selected(row, name, row_num, others=()):
            row = copy.deepcopy(row)
            row['_legacy_selection'] = {'chosen_table':name, 'source_records':[ref(name,row_num), *others]}
            return row
        selected_restored = selected(restored,'realtime_bars',1,[ref('candles',2)])
        selected_restored['state'] = 'provisional_authoritative'; selected_restored['finalized_at'] = None
        selected_restored['_invalid_legacy_finality'] = dict(original_state='final', original_finalized_at=restored['finalized_at'],
                reason='invalid_legacy_finality: finalization precedes structural bucket end; no later same-key confirmation in fixed snapshot')
        rows = [selected(base,'candles',0), selected(quote,'realtime_bars',0), selected(point,'candles',1), selected_restored]
        if mixed:
            value = selected(base,'candles',0); value['open_time'] = '2026-09-30T06:12:00Z'
            value['raw_payload']['derivation'] = 'authoritative_bar_with_quote_overlay'; rows.append(value)
        canonical = source/'canonical.ndjson'; canonical.write_bytes(b''.join(clock.encoded(v) for v in rows))
        manifest = dict(id='fixture',postgres=dict(snapshot='fixed',fingerprint='fixed-source'),tables=tables)
        descriptor = dict(source_manifest_id='fixture',snapshot='fixed',source_fingerprint='fixed-source',source_tables=by_table,
                file=canonical.name,sha256=clock.sha(canonical),rows=str(len(rows)))
        (source/'manifest.json').write_bytes(clock.encoded(manifest))
        (source/'canonical-bars-v1.manifest.json').write_bytes(clock.encoded(descriptor))
        return source

    def execute(self, mixed, early=False):
        with tempfile.TemporaryDirectory(prefix='tracefang-clock-test-') as tmp:
            root = Path(tmp); source = self.fixture(root,mixed,early); before = {p.name:clock.sha(p) for p in source.iterdir()}
            subprocess.run(['python3',str(ROOT/'prepare-legacy-clock-canonical.py'),str(source),str(root/'output'),str(root/'cache'),
                '--policy-probe',os.environ['TRACEFANG_CLOCK_POLICY_PROBE'],'--policy-source',str(ROOT.parent/'backend/src/source_clock.rs'),
                '--policy-evidence',str(source/'manifest.json')],check=True,capture_output=True)
            result = clock.read(root/'output/canonical-bars-clock-v2.manifest.json')
            self.assertEqual(result['complete'],not mixed)
            self.assertEqual(result['counts']['input_rows'],str(5 if mixed else 4))
            self.assertEqual(result['counts']['point_rows'],'1'); self.assertEqual(result['counts']['collided_input_rows'],'1')
            self.assertEqual(result['counts'].get('restored_original_final_rows','0'),'0' if early else '1')
            output = [json.loads(v) for v in (root/'output'/result['file']).read_bytes().splitlines()]
            self.assertEqual(len(output),3 if mixed else 2)
            self.assertEqual([clock.key(v) for v in output],sorted(clock.key(v) for v in output))
            first = output[0]; self.assertEqual(first['open'],'0.0000000000000000000000000001')
            self.assertEqual(first['open_time'],'2026-09-30T06:06:00Z'); self.assertEqual(first['observed_at'],'2026-09-30T06:07:00Z')
            self.assertEqual(first['received_at'],'2026-09-30T06:07:01.123456789Z'); self.assertIsNone(first['volume'])
            restored = output[1]
            self.assertEqual(restored['finalized_at'],None if early else '2026-09-30T06:10:00.000000001Z')
            self.assertIn('original_final_source_record',restored['_legacy_clock_projection'])
            if early:
                proof = restored['_legacy_clock_projection']
                self.assertEqual(restored['state'],'provisional_authoritative')
                self.assertEqual(proof['original_final_not_restored_reason'],'confirmation_precedes_corrected_end')
                self.assertEqual(proof['original_reported_finalized_at'],'2026-09-30T06:09:30.000000001Z')
                self.assertIsNone(proof['original_finalized_at'])
            self.assertEqual(before,{p.name:clock.sha(p) for p in source.iterdir()})

    @unittest.skipUnless(os.environ.get('TRACEFANG_CLOCK_POLICY_PROBE'),'release helper path required')
    def test_collision_point_exact_values_and_real_finality_restore(self): self.execute(False)

    @unittest.skipUnless(os.environ.get('TRACEFANG_CLOCK_POLICY_PROBE'),'release helper path required')
    def test_mixed_lineage_is_preserved_and_blocks_complete(self): self.execute(True)

    @unittest.skipUnless(os.environ.get('TRACEFANG_CLOCK_POLICY_PROBE'),'release helper path required')
    def test_proven_mapping_preserves_preview_when_original_final_is_early(self): self.execute(False,True)


if __name__ == '__main__': unittest.main()
