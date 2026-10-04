#!/usr/bin/env python3
"""Read-only isolated fixed-binary HTTP cold request and twenty warm exact comparisons."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import statistics
import subprocess
import tempfile
import time
import urllib.request


def file_sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for part in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def measure(url, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                    headers={'Content-Type': 'application/json'})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = response.read()
        status = response.status
    parsed = json.loads(payload)
    return parsed, {'ms': (time.perf_counter() - started) * 1000,
                    'status': status, 'response_bytes': len(payload)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('binary', 'facts', 'capture', 'snapshot-root', 'oracle', 'report'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--generation', required=True)
    parser.add_argument('--snapshot-id', required=True)
    parser.add_argument('--port', type=int, default=18030)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    # Bind the binary to the prepared deployment receipt before starting it.
    receipt = json.loads((binary.parent.parent / 'release-manifest.json').read_bytes())
    digest = file_sha(binary)
    if digest != receipt['binary_sha256']:
        raise ValueError('prepared binary digest differs')
    oracle = json.loads(args.oracle.read_bytes())
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1', args.port)) == 0:
            raise ValueError('isolated probe port already in use')
    env = os.environ.copy()
    env.update(TRACEFANG_READ_ONLY_SHADOW='1', TRACEFANG_REHEARSAL_GENERATION=args.generation,
               TRACEFANG_STORE_PATH=str(args.facts.resolve(strict=True)),
               TRACEFANG_CAPTURE_PATH=str(args.capture.resolve(strict=True)),
               TRACEFANG_BATCH_SNAPSHOTS_DIR=str(args.snapshot_root.resolve(strict=True)),
               TRACEFANG_ACQUISITION_ENABLED='0', TRACEFANG_PORT=str(args.port))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    log = args.report.with_suffix('.server.log')
    with log.open('wb') as output:
        process = subprocess.Popen([binary], cwd=binary.parent.parent, env=env,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        try:
            start = time.perf_counter()
            while True:
                if process.poll() is not None:
                    raise RuntimeError('read-only probe process exited before listen; see private startup log')
                with socket.socket() as sock:
                    if sock.connect_ex(('127.0.0.1', args.port)) == 0:
                        break
                if time.perf_counter() - start > 90:
                    raise TimeoutError('isolated readonly startup exceeded budget')
                time.sleep(.1)
            startup = (time.perf_counter() - start) * 1000
            # This is the first HTTP request, with no manifest/runtime warm-up.
            url = f'http://127.0.0.1:{args.port}/api/research/native-snapshots/{args.snapshot_id}/aggregate'
            body = oracle['range']
            cold, first = measure(url, body)
            assert cold['result'] == oracle['result']['result']
            assert cold['engine'] == 'duckdb-1.5.6-decimal38_18'
            samples = []
            for _ in range(20):
                result, timing = measure(url, body)
                assert result == cold
                samples.append(timing)
            times = sorted(value['ms'] for value in samples)
            report = {'kind': 'actual_fixed_deployed_native_HTTP_columnar_cold_warm',
                      'binary_sha256': digest, 'backend_build_fingerprint': receipt['build_info']['backend_build_fingerprint'],
                      'snapshot_id': args.snapshot_id, 'file_sha256': oracle['file_sha256'],
                      'manifest_rows': oracle['manifest_rows'], 'readonly_startup_ms': startup,
                      'cold_first_HTTP_request_includes_snapshot_full_hash_runtime_copy_version_and_worker': first,
                      'warm_HTTP': {'n': 20, 'p50_ms': statistics.median(times),
                                    'p95_ms': times[math.ceil(.95 * len(times)) - 1], 'min_ms': min(times),
                                    'max_ms': max(times), 'samples': samples},
                      'result': cold, 'all_exact_oracle_matches': True,
                      'scope': 'localhost HTTP request through complete response read and JSON decode; includes transport, server validation and worker IPC; cold means process verification cache empty, OS file cache warm; startup listed separately; no fleet builds/repair/root heavy query; background OS load possible; not browser render or exclusive machine SLO',
                      'production_changed': False, 'old_services_stopped': False}
        finally:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
            output.flush(); os.fsync(output.fileno())
    report['isolated_process_exit_code'] = process.returncode
    with tempfile.NamedTemporaryFile('w', dir=args.report.parent, delete=False) as file:
        json.dump(report, file, indent=2); file.flush(); os.fsync(file.fileno()); name = file.name
    os.replace(name, args.report)
    print(json.dumps({'report': str(args.report.resolve()), 'cold_ms': first['ms'],
                      'warm_p50_ms': report['warm_HTTP']['p50_ms'], 'warm_p95_ms': report['warm_HTTP']['p95_ms']}))


if __name__ == '__main__':
    main()
