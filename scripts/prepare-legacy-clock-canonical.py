#!/usr/bin/env python3
"""Bounded, source-evidenced clock projection. Original archives/facts stay unchanged."""
import argparse
import collections
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time


def read(path):
    return json.loads(path.read_bytes(), parse_float=str)


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n').encode()


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def ns(value):
    match = re.fullmatch(r'(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)', value)
    if not match:
        raise ValueError('timestamp must retain exact signed nanosecond precision and timezone')
    instant = datetime.datetime.fromisoformat(match[1] + ('+00:00' if match[3] == 'Z' else match[3]))
    delta = instant.astimezone(datetime.timezone.utc) - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9 + int((match[2] or '').ljust(9, '0'))


def key(row):
    source = row.get('realtime_source_id') or row.get('source_id')
    if source.startswith('jin10_'):
        source = 'jin10_client'
    return source, row['instrument_symbol'], int(row['interval_seconds']), ns(row['open_time'])


def private(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')


def publish(path, value):
    with private(path) as stream:
        stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('cache', type=Path)
    parser.add_argument('--policy-probe', type=Path, required=True)
    parser.add_argument('--policy-evidence', type=Path, action='append', required=True)
    parser.add_argument('--policy-source', type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    phases = {}
    source = args.source.resolve(strict=True)
    manifest = read(source / 'manifest.json')
    descriptor_path = source / 'canonical-bars-v1.manifest.json'
    descriptor = read(descriptor_path)
    source_identities = {source/'manifest.json': identity(source/'manifest.json'), descriptor_path: identity(descriptor_path)}
    if (descriptor['source_manifest_id'], descriptor['snapshot'], descriptor['source_fingerprint']) != (
            manifest['id'], manifest['postgres']['snapshot'], manifest['postgres']['fingerprint']):
        raise ValueError('fixed canonical source belongs to another snapshot')
    if args.output.exists() or args.cache.exists():
        raise ValueError('fresh owned output/cache directories required; partial evidence is preserved')
    args.output.mkdir(parents=True, mode=0o700)
    args.cache.mkdir(parents=True, mode=0o700)
    probe = args.policy_probe.resolve(strict=True)
    source_identities[probe] = identity(probe)
    policy_source = args.policy_source.resolve(strict=True)
    source_identities[policy_source] = identity(policy_source)
    policy = json.loads(subprocess.check_output([str(probe), 'policy'], stdin=subprocess.DEVNULL))
    scopes = {(scope['provider_code'], scope['instrument_symbol']) for scope in policy['policy']['scopes']}
    policy_evidence = []
    for path in args.policy_evidence:
        path = path.resolve(strict=True)
        source_identities[path] = identity(path)
        policy_evidence.append({'file': str(path), 'sha256': sha(path)})
    tables = {table['table']: table for table in manifest['tables']}
    plan_path = args.cache / 'clock-offsets.sqlite'
    connection = sqlite3.connect(plan_path)
    connection.execute('PRAGMA journal_mode=OFF')
    connection.execute('PRAGMA cache_size=-16384')
    connection.execute('CREATE TABLE originals(table_name TEXT,row_number INTEGER,row_json TEXT,row_sha TEXT,PRIMARY KEY(table_name,row_number)) WITHOUT ROWID')
    connection.execute('CREATE TABLE selected(source TEXT,symbol TEXT,interval INTEGER,at INTEGER,input_offset INTEGER,input_row INTEGER,action TEXT,override TEXT,PRIMARY KEY(source,symbol,interval,at)) WITHOUT ROWID')
    counts = collections.Counter()
    reasons = collections.Counter()

    def budget():
        if shutil.disk_usage(args.output).free <= 4 * 1024**3:
            raise RuntimeError('clock projection stopped below 4GiB free; all source/evidence retained')
        if plan_path.exists() and plan_path.stat().st_size > 512 * 1024**2:
            raise RuntimeError('owned offset cache exceeded 512MiB bound; input not truncated')
        if sum(path.stat().st_size for path in args.output.iterdir() if path.is_file()) > 4 * 1024**3:
            raise RuntimeError('clock projection output exceeded 4GiB bound; all partial evidence retained')

    def relevant(row):
        return (row.get('provider_symbol'), row.get('instrument_symbol')) in scopes and int(row.get('interval_seconds', 0)) == 60

    # All original files are hashed and counted during this one streaming pass.
    # Only the reviewed small source scope is materialized in the offset cache.
    for name in ('candles', 'realtime_bars'):
        phase_start = time.perf_counter()
        table = tables[name]
        table_path = (source / table['file']).resolve(strict=True)
        if table_path.parent != source:
            raise ValueError('original table path escapes immutable source directory')
        source_identities[table_path] = identity(table_path)
        h = hashlib.sha256(); rows = 0
        with table_path.open('rb') as stream:
            for number, line in enumerate(stream):
                h.update(line); rows += 1
                row = json.loads(line, parse_float=str)
                if relevant(row):
                    connection.execute('INSERT INTO originals VALUES(?,?,?,?)',
                            (name, number, json.dumps(row, separators=(',', ':')), hashlib.sha256(line.rstrip(b'\n')).hexdigest()))
                if number % 10000 == 0:
                    connection.commit(); budget()
        if h.hexdigest() != table['sha256'] or str(rows) != table['rows']:
            raise ValueError('original source file/count differs: ' + name)
        phases['verify_and_index_'+name+'_ms'] = (time.perf_counter()-phase_start)*1000
        print(json.dumps({'phase':'verified_original_table','table':name,'rows':str(rows)}),flush=True)
    connection.commit()
    input_path = source / descriptor['file']
    source_identities[input_path] = identity(input_path)
    mappings = private(args.output / 'clock-mapping.ndjson')
    points = private(args.output / 'source-points.ndjson')
    collisions = private(args.output / 'clock-collisions.ndjson')
    unresolved = private(args.output / 'clock-unresolved.ndjson')
    worker_log = private(args.output / 'clock-policy-worker.log')
    worker = subprocess.Popen([str(probe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=worker_log)
    h = hashlib.sha256(); offset = 0
    phase_start = time.perf_counter()

    def resolved(row):
        selected = row['_legacy_selection']
        history = original = original_ref = None
        original_sha = None
        history_ref = None; history_sha = ''
        for reference in selected['source_records']:
            table = tables.get(reference['table'])
            if table is None or (reference['file'], reference['sha256']) != (table['file'], table['sha256']):
                raise ValueError('source_records references a different immutable table')
            found = connection.execute('SELECT row_json,row_sha FROM originals WHERE table_name=? AND row_number=?',
                    (reference['table'], int(reference['row_offset']))).fetchone()
            if found:
                value = json.loads(found[0])
                if reference['table'] == 'candles':
                    history, history_ref, history_sha = value, reference, found[1]
                if reference['table'] == selected['chosen_table']:
                    original, original_ref, original_sha = value, reference, found[1]
        return history, original, history_ref or original_ref, history_sha, original_ref, original_sha

    def input_at(stream, found):
        if found[3] is not None:
            return json.loads(found[3])
        stream.seek(found[0]); return json.loads(stream.readline(), parse_float=str)

    def quote(row):
        raw = row.get('source_raw_payload') or row.get('raw_payload') or {}
        return raw.get('derivation') == 'quote_event'

    try:
        with input_path.open('rb') as stream, input_path.open('rb') as previous:
            for row_number, line in enumerate(stream):
                counts['input_rows'] += 1; h.update(line)
                row = json.loads(line, parse_float=str)
                decision = {'decision': 'unchanged', 'reason': 'outside_reviewed_v6_authoritative_minute_scope'}
                if relevant(row):
                    history, original, source_ref, source_row_sha, original_ref, original_sha = resolved(row)
                    request = {'row': row, 'history': history, 'original': original, 'source_ref': source_ref,
                               'source_row_sha256': source_row_sha, 'original_source_ref': original_ref, 'original_source_row_sha256': original_sha}
                    worker.stdin.write(encoded(request)); worker.stdin.flush()
                    answer = worker.stdout.readline()
                    if not answer:
                        raise RuntimeError('bounded clock-policy worker ended before all source rows')
                    decision = json.loads(answer)
                    counts['reviewed_scope_rows'] += 1
                action = decision['decision']
                if action == 'shifted' and decision['evidence'].get('restored_prior_final_state'):
                    counts['restored_original_final_rows'] += 1
                counts[action + '_rows'] += 1
                reasons[decision.get('reason') or action] += 1
                if relevant(row):
                    record = {'input_row_offset': str(row_number), 'input_byte_offset': str(offset),
                              'original_key': key(row), **decision}
                    # Mapping references the original row; shifted values are kept
                    # once in the final canonical file, not duplicated in this ledger.
                    record.pop('row', None)
                    if action == 'shifted':
                        record['corrected_key'] = key(decision['row'])
                    mappings.write(encoded(record))
                if action == 'point':
                    points.write(encoded({'input_row_offset': str(row_number), **decision}))
                    if decision['unresolved']:
                        counts['unresolved_differences'] += 1
                        unresolved.write(encoded({'input_row_offset': str(row_number), **decision}))
                    offset += len(line)
                    continue
                if action == 'unresolved':
                    counts['unresolved_differences'] += 1
                    unresolved.write(encoded({'input_row_offset': str(row_number), 'source_records': row['_legacy_selection']['source_records'], **decision}))
                candidate = decision.get('row', row)
                k = key(candidate)
                override = json.dumps(candidate, separators=(',', ':')) if action == 'shifted' else None
                found = connection.execute('SELECT input_offset,input_row,action,override FROM selected WHERE source=? AND symbol=? AND interval=? AND at=?', k).fetchone()
                if found:
                    older = input_at(previous, found)
                    previous_wins = (found[2] == 'shifted' and (older.get('state') in (None, 'final'))
                            and quote(candidate) and candidate.get('state') in ('forming', 'provisional_quote'))
                    current_wins = (action == 'shifted' and (candidate.get('state') in (None, 'final'))
                            and quote(older) and older.get('state') in ('forming', 'provisional_quote'))
                    resolution = ('evidenced_complete_history_preserves_precedence_over_quote_preview' if previous_wins or current_wins
                                  else 'unresolved_new_key_collision')
                    counts['collided_input_rows'] += 1
                    if not (previous_wins or current_wins):
                        counts['unresolved_differences'] += 1
                    collisions.write(encoded({'corrected_key': k, 'previous_input_row': str(found[1]),
                            'current_input_row': str(row_number), 'previous_source_records': older['_legacy_selection']['source_records'],
                            'current_source_records': candidate['_legacy_selection']['source_records'], 'resolution': resolution,
                            'selected_input_row': str(row_number if current_wins else found[1])}))
                    if current_wins:
                        connection.execute('UPDATE selected SET input_offset=?,input_row=?,action=?,override=? WHERE source=? AND symbol=? AND interval=? AND at=?', (offset, row_number, action, override, *k))
                else:
                    connection.execute('INSERT INTO selected VALUES(?,?,?,?,?,?,?,?)', (*k, offset, row_number, action, override))
                if row_number % 10000 == 0:
                    connection.commit(); budget()
                offset += len(line)
        connection.commit(); worker.stdin.close()
        if worker.wait(timeout=30) != 0:
            raise RuntimeError('clock-policy worker failed; all original and partial outputs retained')
        if h.hexdigest() != descriptor['sha256'] or str(counts['input_rows']) != descriptor['rows']:
            raise ValueError('canonical input count/hash differs from frozen source')
        phases['source_evidenced_projection_and_collision_selection_ms'] = (time.perf_counter()-phase_start)*1000
        print(json.dumps({'phase':'selected_corrected_keys','counts':dict(counts)}),flush=True)
        phase_start = time.perf_counter()
        output_hash = hashlib.sha256(); key_hash = hashlib.sha256()
        output_path = args.output / 'canonical-bars-clock-v2.ndjson'
        with private(output_path) as output, input_path.open('rb') as original:
            for selected in connection.execute('SELECT input_offset,input_row,action,override FROM selected ORDER BY source,symbol,interval,at'):
                row = input_at(original, selected)
                if row.get('_legacy_clock_projection'):
                    row['_legacy_clock_projection']['original_canonical_row_offset'] = str(selected[1])
                    row['_legacy_clock_projection']['original_canonical_file_sha256'] = descriptor['sha256']
                value = encoded(row); output.write(value); output_hash.update(value); key_hash.update(encoded(key(row)))
                counts['output_rows'] += 1
                if counts['output_rows'] % 10000 == 0:
                    budget()
            output.flush(); os.fsync(output.fileno())
        for file in (mappings, points, collisions, unresolved):
            file.flush(); os.fsync(file.fileno()); file.close()
        if counts['input_rows'] != counts['output_rows'] + counts['point_rows'] + counts['collided_input_rows']:
            raise RuntimeError('clock projection row conservation failed')
        budget()
        phases['sorted_canonical_publish_ms'] = (time.perf_counter()-phase_start)*1000
        for path, expected in source_identities.items():
            if identity(path) != expected:
                raise RuntimeError('immutable source, witness, or policy worker changed during clock projection: ' + str(path))
        result = {'schema': 'legacy-source-clock-projection-v2', 'policy_version': 'legacy-bars-fixed-authority+clock-projection-v2',
                'policy': policy, 'policy_probe_sha256': sha(probe), 'policy_evidence': policy_evidence,
                'policy_source_sha256': sha(policy_source), 'policy_source_file': str(policy_source),
                'started_at':started_at,'completed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'phase_ms':phases,'total_ms':(time.perf_counter()-started)*1000,
                'source_manifest_id': manifest['id'], 'snapshot': manifest['postgres']['snapshot'],
                'source_fingerprint': manifest['postgres']['fingerprint'], 'original_descriptor_sha256': sha(descriptor_path),
                'original_canonical_file_sha256': descriptor['sha256'], 'source_tables': descriptor['source_tables'],
                'file': output_path.name, 'sha256': output_hash.hexdigest(), 'rows': str(counts['output_rows']),
                'sorted_unique_key_sha256': key_hash.hexdigest(), 'counts': {k: str(v) for k, v in counts.items()},
                'reason_counts': dict(reasons), 'complete': counts['unresolved_differences'] == 0,
                'activation': False, 'original_source_or_facts_modified': False, 'quotes_shifted': False,
                'artifacts': {name: {'file': name, 'sha256': sha(args.output/name)} for name in
                        ('clock-mapping.ndjson', 'source-points.ndjson', 'clock-collisions.ndjson', 'clock-unresolved.ndjson')},
                'count_semantics': 'fixed canonical input rows = corrected canonical selected rows + separately retained point rows + colliding nonselected input rows; source event/tick counts are not inferred'}
        publish(args.output / 'canonical-bars-clock-v2.manifest.json', result)
        print(json.dumps({'descriptor': str(args.output/'canonical-bars-clock-v2.manifest.json'), 'complete': result['complete'], 'counts': result['counts']}))
    finally:
        for file in (mappings, points, collisions, unresolved):
            if not file.closed:
                file.flush(); file.close()
        if worker.poll() is None:
            worker.terminate(); worker.wait(timeout=30)
        worker_log.flush(); os.fsync(worker_log.fileno()); worker_log.close()
        connection.close()


if __name__ == '__main__':
    main()
