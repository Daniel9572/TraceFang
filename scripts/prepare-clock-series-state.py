#!/usr/bin/env python3
"""Read-only exact oracle for corrected legacy authority recovery watermarks."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import runpy
import time

_shared = runpy.run_path(str(Path(__file__).with_name('prepare-legacy-clock-canonical.py')))
ns, sha, identity, publish = (_shared[name] for name in ('ns', 'sha', 'identity', 'publish'))
POLICY = 'ths-v6-period61-shfe-interval-end-v2'
SCOPES = {'AU2610': 'qh_au2610', 'AU8888': 'qh_au8888',
          'AG2706': 'qh_ag2706', 'AG8888': 'qh_ag8888'}


def derive(row, reference, group):
    symbol = row['instrument_symbol']
    evidence = row.get('_legacy_clock_projection') or {}
    source = row.get('realtime_source_id') or row.get('source_id')
    if (source != 'tonghuashun_futures'
            or int(row['interval_seconds']) != 60 or symbol not in SCOPES
            or evidence.get('policy_id') != POLICY):
        return
    if evidence['provider_code'] != SCOPES[symbol] or not evidence.get('original_source_record'):
        raise ValueError('corrected authority scope lacks exact original lineage')
    opening = ns(row['open_time'])
    closing = opening + 60 * 10**9
    if row.get('close_time') and ns(row['close_time']) != closing:
        raise ValueError('corrected authority interval mismatch')
    state = row.get('state', 'final')
    group['authority_rows'] += 1
    group['states'][state] = group['states'].get(state, 0) + 1
    received = ns(row['received_at'])
    if group.get('received_ns') is None or received > group['received_ns']:
        group['received_ns'], group['received_at'] = received, row['received_at']
        group['received_ref'] = reference
    if state != 'final':
        return
    if row.get('finalized_at') is None:
        group['final_clock_unknown_rows'] += 1
        return
    finalized = ns(row['finalized_at'])
    if finalized < closing:
        raise ValueError('Final authority confirmation precedes corrected end')
    group['confirmed_authority_rows'] += 1
    if group.get('close_ns') is None or closing > group['close_ns']:
        group.update(close_ns=closing, open_ns=opening, open_time=row['open_time'],
                     finalized_at=row['finalized_at'], received_at_at_boundary=row['received_at'],
                     provider_symbol=row['provider_symbol'],
                     upstream_channel_id=row.get('evidence_channel_id') or row.get('upstream_channel_id') or source,
                     boundary_ref=reference)


def iso(value):
    seconds, nanos = divmod(value, 10**9)
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + f'.{nanos:09d}Z'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('clock', type=Path)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.source/'manifest.json').read_bytes())
    plan_path = args.clock/'canonical-bars-clock-v2.manifest.json'
    plan = json.loads(plan_path.read_bytes())
    if not (plan['complete'] and plan['source_manifest_id'] == manifest['id']
            and plan['snapshot'] == manifest['postgres']['snapshot']):
        raise ValueError('incomplete projection or different fixed snapshot')
    state_table = next(v for v in manifest['tables'] if v['table'] == 'realtime_bar_series_state')
    state_path = args.source/state_table['file']
    if sha(state_path) != state_table['sha256']:
        raise ValueError('original state archive changed')
    original = {}
    for number, line in enumerate(state_path.open('rb')):
        value = json.loads(line)
        if value['realtime_source_id'] == 'tonghuashun_futures' and value['instrument_symbol'] in SCOPES:
            original[value['instrument_symbol']] = {'row': value, 'reference': {
                'file': state_table['file'], 'file_sha256': state_table['sha256'],
                'row_offset': str(number), 'row_sha256': hashlib.sha256(line.rstrip(b'\n')).hexdigest()}}
    groups = {symbol: {'authority_rows': 0, 'states': {}, 'confirmed_authority_rows': 0,
                       'final_clock_unknown_rows': 0} for symbol in SCOPES}
    path = args.clock/plan['file']; before = identity(path)
    digest = hashlib.sha256(); count = 0; offset = 0; started = time.monotonic()
    with path.open('rb') as stream:
        for number, line in enumerate(stream):
            digest.update(line); count += 1
            row = json.loads(line, parse_float=str)
            symbol = row['instrument_symbol']
            if symbol in groups:
                derive(row, {'file': str(path), 'file_sha256': plan['sha256'],
                       'row_offset': str(number), 'byte_offset': str(offset),
                       'row_sha256': hashlib.sha256(line.rstrip(b'\n')).hexdigest(),
                       'source_clock_projection': row.get('_legacy_clock_projection')}, groups[symbol])
            offset += len(line)
    if digest.hexdigest() != plan['sha256'] or str(count) != plan['rows'] or identity(path) != before:
        raise ValueError('complete corrected canonical bytes/count/identity changed')
    scopes = []
    for symbol, group in groups.items():
        if not group.get('confirmed_authority_rows') or symbol not in original:
            raise ValueError('scope lacks proven Final authority or original state')
        state = {'realtime_source_id': 'tonghuashun_futures', 'instrument_symbol': symbol,
                 'upstream_channel_id': group['upstream_channel_id'], 'provider_symbol': group['provider_symbol'],
                 'interval': 60, 'latest_authoritative_open_time': iso(group['open_ns']),
                 'authoritative_through': iso(group['close_ns']), 'history_floor': None,
                 'tail_checked_through': None, 'tail_checked_at': None,
                 'evidence_version': POLICY + ':' + sha(plan_path), 'updated_at': group['received_at']}
        scopes.append({'symbol': symbol, 'original': original[symbol], 'candidate': state,
                       'derivation': group})
    publish(args.report, {'schema': 'independent-clock-series-state-v1', 'complete': True,
            'source_manifest_id': manifest['id'], 'snapshot': manifest['postgres']['snapshot'],
            'policy': POLICY, 'clock_manifest_sha256': sha(plan_path),
            'canonical_sha256': digest.hexdigest(), 'all_canonical_rows_scanned': str(count),
            'elapsed_ms': (time.monotonic()-started)*1000, 'scopes': scopes,
            'history_floor_semantics': 'None: no proof of uninterrupted complete historical coverage',
            'tail_check_semantics': 'None: old label-clock check is not a corrected independent tailcheck',
            'facts_modified': False, 'production_modified': False})
    print(json.dumps({'report': str(args.report), 'rows': str(count),
                      'boundaries': {v['symbol']: v['candidate'] for v in scopes}}), flush=True)


if __name__ == '__main__':
    main()
