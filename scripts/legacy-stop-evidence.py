#!/usr/bin/env python3
"""Inspect or stop the exact installed launchd collector and record real observations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.pending')
    with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as file:
        json.dump(value, file, indent=2); file.flush(); os.fsync(file.fileno())
    os.replace(temporary, path)


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def stable_read(path, limit=64 * 1024 * 1024):
    path = Path(path)
    before = os.stat(path, follow_symlinks=False)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'evidence input must be a regular non-symlink file: {path}')
    chunks = []
    size = 0
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError(f'evidence input changed before read: {path}')
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > limit:
                raise ValueError(f'evidence input exceeds the read bound: {path}')
            chunks.append(block)
        fd_after = os.fstat(stream.fileno())
    after = os.stat(path, follow_symlinks=False)
    if size != before.st_size or _identity(fd_after) != _identity(before) or _identity(after) != _identity(before):
        raise ValueError(f'evidence input was short-read or changed: {path}')
    return b''.join(chunks)


def stable_sha(path):
    path = Path(path)
    before = os.stat(path, follow_symlinks=False)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'evidence input must be a regular non-symlink file: {path}')
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError(f'evidence input changed before hashing: {path}')
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
        fd_after = os.fstat(stream.fileno())
    after = os.stat(path, follow_symlinks=False)
    if size != before.st_size or _identity(fd_after) != _identity(before) or _identity(after) != _identity(before):
        raise ValueError(f'evidence input was short-hashed or changed: {path}')
    return digest.hexdigest()


def http(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        data = response.read(1024*1024 + 1)
        if len(data) > 1024*1024:
            raise ValueError('health response exceeds evidence bound')
        return json.loads(data)


def job(domain):
    process = subprocess.run(['launchctl', 'print', domain], capture_output=True, text=True)
    pid = re.search(r'^\s*pid = (\d+)\s*$', process.stdout, re.MULTILINE)
    # Do not serialize launchctl's full output: it can include environment secrets.
    return {'registered': process.returncode == 0, 'pid': int(pid[1]) if pid else None}


def processes():
    output = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,comm='], text=True)
    rows = {}
    for line in output.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) == 3:
            rows[int(fields[0])] = {'pid': int(fields[0]), 'parent_pid': int(fields[1]), 'executable': fields[2]}
    return rows


def identity(pid):
    value = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=,comm='], capture_output=True, text=True)
    if value.returncode or not value.stdout.strip():
        return None
    return sha_bytes(value.stdout.strip().encode())


def tree(rows, root):
    wanted = {root}
    while True:
        added = {pid for pid, row in rows.items() if row['parent_pid'] in wanted}
        if added <= wanted:
            return sorted(wanted)
        wanted |= added


def identity_tree(rows, root):
    pids = tree(rows, root)
    result = []
    for pid in pids:
        row = rows.get(pid)
        start = identity(pid)
        if row is None or start is None:
            raise ValueError('installed process tree changed while its start identities were sampled')
        result.append({'pid': pid, 'start_identity_sha256': start, **row})
    return result


def artifact_receipt(path):
    path = Path(path).resolve(strict=True)
    return {'file': path.name, 'sha256': stable_sha(path)}


def stable_stop_observations(domain, bound, hostname, port, stable_seconds):
    deadline = time.monotonic() + stable_seconds
    observations = []
    while True:
        current = job(domain)
        if current['registered']:
            raise RuntimeError('old launchd job revived; keep native providers stopped')
        if any(identity(row['pid']) == row['start_identity_sha256'] for row in bound):
            raise RuntimeError('old process tree revived; keep native providers stopped')
        try:
            connection = socket.create_connection((hostname, port), timeout=1)
        except ConnectionRefusedError:
            pass
        except OSError as error:
            raise RuntimeError('unable to prove old listener closed; keep native providers stopped') from error
        else:
            connection.close()
            raise RuntimeError('old listener revived or port reused; keep native providers stopped')
        observations.append({'observed_at_ns': str(time.time_ns()), 'job_registered': False,
                             'ready_endpoint_closed': True, 'process_tree_exited': True})
        if (time.monotonic() >= deadline
                and int(observations[-1]['observed_at_ns']) - int(observations[0]['observed_at_ns'])
                    >= int(stable_seconds * 1_000_000_000)):
            return observations
        time.sleep(.25)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('inspect', 'stop'))
    parser.add_argument('--plist', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--expected-plist-sha256')
    parser.add_argument('--inspection-report', type=Path)
    parser.add_argument('--inspection-sha256')
    parser.add_argument('--stable-seconds', type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.stable_seconds <= 60:
        raise ValueError('stable interval must be 1..60 seconds')
    parsed = urlsplit(args.base_url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('only the local installed collector may be controlled')
    plist_path = args.plist.resolve(strict=True)
    raw = stable_read(plist_path); digest = sha_bytes(raw); plist = plistlib.loads(raw)
    if plist.get('Label') != 'com.tracefang.local' or not plist.get('KeepAlive'):
        raise ValueError('unrecognized launchd job; no process changed')
    domain = f'gui/{os.getuid()}/com.tracefang.local'
    registration = job(domain)
    if not registration['registered'] or not registration['pid']:
        raise ValueError('installed legacy launchd job is not running')
    ready = http(args.base_url.rstrip('/') + '/api/ready')
    health = http(args.base_url.rstrip('/') + '/api/health')
    rows = processes(); bound = identity_tree(rows, registration['pid'])
    pids = [row['pid'] for row in bound]
    if ready['process_id'] not in pids:
        raise ValueError('ready endpoint belongs to a different process tree')
    argument = Path(plist['ProgramArguments'][0])
    installed = argument.parent.parent.parent
    inputs = []
    for relative in ('src/tracefang/infrastructure/postgres/writer.py', 'src/tracefang/api.py'):
        path = installed / relative
        if path.is_file():
            inputs.append({'file': str(path), 'sha256': stable_sha(path)})
    args.evidence.mkdir(parents=True, exist_ok=True)
    receipt = {'schema': 'legacy-installed-stop-inspection-v1', 'observed_at_ns': str(time.time_ns()),
               'plist_path': str(plist_path), 'plist_sha256': digest, 'label': plist['Label'],
               'keep_alive': plist.get('KeepAlive'), 'run_at_load': plist.get('RunAtLoad'),
               'domain': domain, 'registration': registration, 'processes': bound,
               'ready': ready, 'writer_health_before': health.get('database'), 'installed_inputs': inputs,
               'writer_raw_applied_watermark_available': False,
               'queue_depth_excludes_current_pending_request': True, 'processes_changed': False}
    if args.mode == 'inspect':
        report = args.evidence / ('installed-stop-inspection-' + receipt['observed_at_ns'] + '.json')
        save(report, receipt)
        print(json.dumps({'inspection': str(report.resolve()), 'sha256': stable_sha(report), 'processes_changed': False}))
        return
    if args.inspection_report is None or args.inspection_sha256 is None:
        raise ValueError('stop requires an independent immutable inspection receipt and its SHA')
    inspection = args.inspection_report.resolve(strict=True)
    args.evidence = args.evidence.resolve(strict=True)
    if inspection.parent != args.evidence:
        raise ValueError('inspection receipt must be in the same evidence directory')
    inspected_bytes = stable_read(inspection)
    if sha_bytes(inspected_bytes) != args.inspection_sha256:
        raise ValueError('independent inspection receipt SHA differs; no process changed')
    inspected = json.loads(inspected_bytes)
    keys = ('schema', 'plist_path', 'plist_sha256', 'label', 'domain', 'registration', 'processes', 'installed_inputs')
    if any(receipt[key] != inspected.get(key) for key in keys) or ready['process_id'] != inspected.get('ready', {}).get('process_id'):
        raise ValueError('installed job/PID/start tree differs from independent inspection; no process changed')
    if args.expected_plist_sha256 != digest:
        raise ValueError('stop requires the independently inspected exact installed plist SHA; no process changed')
    if any(identity(row['pid']) != row['start_identity_sha256'] for row in bound):
        raise ValueError('process identity changed since inspection; no process changed')
    if stable_sha(plist_path) != digest:
        raise ValueError('service installation changed before bootout')
    if job(domain) != inspected['registration']:
        raise ValueError('launchd job identity changed immediately before bootout')
    fresh_ready = http(args.base_url.rstrip('/') + '/api/ready')
    fresh_health = http(args.base_url.rstrip('/') + '/api/health')
    fresh_registration = job(domain)
    fresh_processes = identity_tree(processes(), fresh_registration['pid'])
    if (fresh_registration != inspected['registration']
            or fresh_processes != inspected['processes']
            or fresh_ready.get('process_id') != inspected.get('ready', {}).get('process_id')):
        raise ValueError('fresh prebootout registration, PID/start tree or ready PID differs from the inspection')
    pre_stop = {'schema': 'legacy-installed-prebootout-observation-v1',
                'observed_at_ns': str(time.time_ns()), 'plist_sha256': digest,
                'domain': domain, 'registration': fresh_registration,
                'processes': fresh_processes, 'ready': fresh_ready,
                'writer_health': fresh_health.get('database'), 'processes_changed': False,
                'inspection_report_sha256': args.inspection_sha256}
    pre_stop_path = args.evidence/'pre-stop-observation.json'
    save(pre_stop_path, pre_stop)
    # Remove automatic restart registration first; SIGTERM to only the API child would restart it.
    attempt = {'schema': 'legacy-stop-attempt-v1', 'domain': domain, 'plist_sha256': digest,
               'bootout_requested_at_ns': str(time.time_ns()), 'phase': 'bootout_requested',
               'raw_projection_drain_proven': False, 'native_providers_started': False,
               'inspection_report_sha256': args.inspection_sha256,
               'pre_stop_observation_sha256': stable_sha(pre_stop_path)}
    attempt_path = args.evidence/'stop-attempt.json'
    save(attempt_path, attempt)
    result = subprocess.run(['launchctl', 'bootout', domain], check=False, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bootout_exit_code = result.returncode if result is not None else 0
    attempt['bootout_exit_code'] = bootout_exit_code
    attempt['bootout_completed_at_ns'] = str(time.time_ns())
    attempt['phase'] = 'job_booted_out_waiting_tree_and_port' if bootout_exit_code == 0 else 'bootout_failed'
    save(attempt_path, attempt)
    if bootout_exit_code != 0:
        raise RuntimeError(f'launchctl bootout failed with exit code {bootout_exit_code}; producers remain unchanged')
    deadline = time.monotonic() + 90
    while any(identity(row['pid']) == row['start_identity_sha256'] for row in bound):
        if time.monotonic() > deadline:
            raise RuntimeError('old process tree did not terminate; no clean stop report generated; keep native providers stopped')
        time.sleep(.25)
    observations = stable_stop_observations(
        domain, bound, parsed.hostname, parsed.port or 80, args.stable_seconds)
    attempt.update({'phase': 'stopped_process_tree_and_listener_verified',
                    'native_providers_started': False, 'process_tree_exited': True,
                    'listener_closed': True, 'stable_no_restart_observations': observations,
                    'verified_at_ns': str(time.time_ns())})
    save(attempt_path, attempt)
    component_ids = [domain + ':plist:' + digest] + [f"pid:{row['pid']}:start:{row['start_identity_sha256']}" for row in bound]
    stopped = {'schema': 'legacy-stop-evidence-v1', 'production_terminal': True,
               'stopped_component_ids': component_ids, 'raw_producers_stopped': True,
               'observed_at_ns': str(time.time_ns()), 'method': 'launchd_bootout_exact_job_then_process_tree_and_listener_exit',
               'plist_sha256': digest, 'plist_preserved': plist_path.is_file() and stable_sha(plist_path) == digest,
               'stable_no_restart_observations': observations,
               'stable_interval_seconds': args.stable_seconds,
               'domain': domain, 'inspection_report_path': str(inspection),
               'inspection_report_sha256': args.inspection_sha256,
               'inspection_registration': inspected['registration'],
               'inspection_processes': inspected['processes'],
               'prebootout': {'observed_at_ns': pre_stop['observed_at_ns'],
                              'plist_sha256': digest, 'registration': fresh_registration,
                              'processes': fresh_processes, 'bootout_exit_code': bootout_exit_code},
               'process_tree_exited': True, 'listener_closed': True, 'no_resurrection': True,
               'evidence': {'inspection': artifact_receipt(inspection),
                            'pre_stop_observation': artifact_receipt(pre_stop_path),
                            'attempt': artifact_receipt(attempt_path)},
               'scope': 'known installed old collector tree; independent stable raw tail proof remains required'}
    save(args.evidence/'stop.json', stopped)
    save(args.evidence/'drain-diagnostic.json', {'schema': 'legacy-drain-diagnostic-v1',
               'production_terminal': True, 'observed_at_ns': stopped['observed_at_ns'],
               'method': 'installed_writer_cancels_without_queue_join', 'raw_applied_through_legacy': None,
               'unresolved_frames': None, 'complete': False, 'writer_health_before': health.get('database'),
               'reconciliation_required': True, 'not_accepted_as_authority_drain': True})
    print(json.dumps({'stop_report': str((args.evidence/'stop.json').resolve()),
                      'clean_drain_proven': False, 'reconciliation_required': True,
                      'native_providers_started': False}))

if __name__ == '__main__':
    main()
