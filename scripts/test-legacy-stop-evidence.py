#!/usr/bin/env python3
"""No real process/network actions: verifies bootout, wrong-job and pending-write boundaries."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
import sys

spec = importlib.util.spec_from_file_location('legacy_stop', Path(__file__).with_name('legacy-stop-evidence.py'))
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class StopEvidence(unittest.TestCase):
    def exercise(self, correct_sha=True, listener_closed=True, restarted=False):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); root = Path(temp.name)
        plist = root/'service.plist'; raw = plistlib.dumps({'Label': 'com.tracefang.local', 'KeepAlive': True,
                    'RunAtLoad': True, 'ProgramArguments': [str(root/'old-release/.venv/bin/python'), '-m', 'tracefang.service', 'run']})
        plist.write_bytes(raw); digest = hashlib.sha256(raw).hexdigest()
        current = {'stopped': False, 'time': 1.0, 'calls': [], 'restarted': False}
        def bootout(command, **_):
            self.assertEqual(command, ['launchctl', 'bootout', f'gui/{os.getuid()}/com.tracefang.local'])
            current['calls'].append(command); current['stopped'] = True
        def http(url):
            return {'process_id': 11} if url.endswith('/api/ready') else {'database': {'state': 'healthy', 'queue_depth': 0, 'last_write_at': 'original'}}
        def connection(*_, **__):
            if listener_closed:
                raise ConnectionRefusedError()
            return type('Connection', (), {'close': lambda self: None})()
        argv = ['tool', 'stop', '--plist', str(plist), '--evidence', str(root/'evidence'),
                '--expected-plist-sha256', digest if correct_sha else '0'*64, '--stable-seconds', '1']
        with patch.object(sys, 'argv', argv), patch.object(module, 'job', side_effect=lambda _: {'registered': not current['stopped'], 'pid': 10 if not current['stopped'] else None}), \
             patch.object(module, 'processes', return_value={10: {'pid': 10, 'parent_pid': 1, 'executable': 'python'}, 11: {'pid': 11, 'parent_pid': 10, 'executable': 'python'}}), \
             patch.object(module, 'identity', side_effect=lambda pid: None if current['stopped'] else ('restarted:' if current['restarted'] else '') + str(pid)), \
             patch.object(module, 'http', side_effect=http), patch.object(module.subprocess, 'run', side_effect=bootout), \
             patch.object(module.socket, 'create_connection', side_effect=connection), \
             patch.object(module.time, 'monotonic', side_effect=lambda: current['time']), \
             patch.object(module.time, 'sleep', side_effect=lambda seconds: current.__setitem__('time', current['time']+seconds)):
            with patch.object(sys, 'argv', ['tool', 'inspect', '--plist', str(plist), '--evidence', str(root/'evidence')]):
                module.main()
            inspection = next((root/'evidence').glob('installed-stop-inspection-*.json'))
            argv.extend(['--inspection-report', str(inspection), '--inspection-sha256', hashlib.sha256(inspection.read_bytes()).hexdigest()])
            current['restarted'] = restarted
            if not correct_sha or not listener_closed or restarted:
                with self.assertRaises((ValueError, RuntimeError)):
                    module.main()
            else:
                module.main()
        return root, current
    def test_bootout_controls_keepalive_but_never_fabricates_raw_drain(self):
        root, current = self.exercise()
        self.assertEqual(len(current['calls']), 1)
        stop = json.loads((root/'evidence/stop.json').read_text())
        diagnostic = json.loads((root/'evidence/drain-diagnostic.json').read_text())
        self.assertTrue(stop['plist_preserved']); self.assertTrue(stop['raw_producers_stopped'])
        self.assertIsNone(diagnostic['raw_applied_through_legacy']); self.assertIsNone(diagnostic['unresolved_frames'])
        self.assertFalse(diagnostic['complete']); self.assertTrue(diagnostic['reconciliation_required'])
        self.assertEqual(diagnostic['writer_health_before']['queue_depth'], 0)
    def test_wrong_installed_sha_changes_no_process(self):
        root, current = self.exercise(correct_sha=False)
        self.assertFalse(current['calls']); self.assertFalse((root/'evidence/stop.json').exists())
    def test_same_plist_restarted_process_cannot_use_old_inspection(self):
        root, current = self.exercise(restarted=True)
        self.assertFalse(current['calls']); self.assertFalse((root/'evidence/stop.json').exists())
    def test_bound_or_reused_port_never_emits_clean_stop(self):
        root, current = self.exercise(listener_closed=False)
        self.assertTrue(current['calls']); self.assertFalse((root/'evidence/stop.json').exists())
        attempt = json.loads((root/'evidence/stop-attempt.json').read_text())
        self.assertEqual(attempt['phase'], 'job_booted_out_waiting_tree_and_port')
        self.assertFalse(attempt['raw_projection_drain_proven'])

if __name__ == '__main__':
    unittest.main()
