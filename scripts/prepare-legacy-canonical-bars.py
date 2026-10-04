#!/usr/bin/env python3
"""Reviewed fixed-snapshot bar selection; bounded SQLite offsets, no live reads."""
import collections, datetime, decimal, hashlib, json, os, pathlib, sqlite3, sys, shutil
archive, cache = map(pathlib.Path, sys.argv[1:3])
manifest = json.loads((archive / 'manifest.json').read_text())
tables = {entry['table']: entry for entry in manifest['tables']}
policy = 'legacy-bars-fixed-authority-v1'

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()

def ns(value):
    try:
        at = datetime.datetime.fromisoformat(value)
    except ValueError:
        at = datetime.datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%f%z' if '.' in value else '%Y-%m-%dT%H:%M:%S%z')
    delta = at.astimezone(datetime.timezone.utc) - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000

def source(row):
    raw = row.get('realtime_source_id') or row.get('source_id')
    return 'jin10_client' if raw.startswith('jin10_') else raw

def key(row):
    return json.dumps([source(row), row['instrument_symbol'], row['interval_seconds'], ns(row['open_time'])], separators=(',', ':'))

def equal(a, b):
    return all((a.get(k) is None and b.get(k) is None) or (a.get(k) is not None and b.get(k) is not None and decimal.Decimal(str(a[k])) == decimal.Decimal(str(b[k]))) for k in ['open', 'high', 'low', 'close', 'volume'])

snapshot_ns = ns(manifest['postgres']['captured_at'])
def authoritative_history(row, require_received_end):
    raw = row.get('raw_payload') or {}
    recognized = (row['source_id'].startswith('jin10_') and raw.get('bar_state') == 'final' and bool(raw.get('history_file'))) or (row['source_id'] == 'tonghuashun_futures' and raw.get('history_file') == 'tonghuashun_public_line_61_year')
    end = ns(row['open_time']) + int(row['interval_seconds']) * 10**9
    if not recognized or end > snapshot_ns or (require_received_end and end > ns(row['received_at'])):
        raise RuntimeError('unproven historical authority/finality; fixed archive retained')

for name in ['candles', 'realtime_bars']:
    if digest(archive / tables[name]['file']) != tables[name]['sha256']:
        raise RuntimeError('source archive checksum differs')
cache.mkdir(parents=True, exist_ok=True)
database = cache / 'canonical-offsets.sqlite'
if database.exists():
    raise SystemExit('derived canonical plan already exists; preserve or explicitly remove owned cache before retry')
connection = sqlite3.connect(database)
connection.execute('PRAGMA journal_mode=OFF')
connection.execute('PRAGMA cache_size=-16384')
connection.execute('CREATE TABLE offsets(k TEXT PRIMARY KEY, byte_offset INTEGER, row_offset INTEGER, selected INTEGER DEFAULT 0) WITHOUT ROWID')
def budget():
    if shutil.disk_usage(archive).free <= 4*1024**3:
        raise RuntimeError('canonical selection stopped below4GiB free; preserved original input and partial evidence')
    if database.stat().st_size > 1024**3:
        raise RuntimeError('canonical offset scratch exceeded1GiB; original input not truncated')
budget()
candle_path = archive / tables['candles']['file']
with candle_path.open('rb') as input_file:
    offset = 0
    for index, line in enumerate(input_file):
        row = json.loads(line, parse_float=str)
        connection.execute('INSERT INTO offsets(k,byte_offset,row_offset) VALUES (?,?,?)', (key(row), offset, index))
        offset += len(line)
        if index % 10000 == 0:
            connection.commit(); budget()
connection.commit()
output_path = archive / 'canonical-bars-v1.ndjson'
output = os.fdopen(os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')
selected = collections.Counter()
states = collections.Counter()
invalid_finality = []
h = hashlib.sha256()
count = 0
def ref(table, offset):
    return {'table': table, 'file': tables[table]['file'], 'sha256': tables[table]['sha256'], 'row_offset': str(offset)}

def publish(row, chosen_table, reason, refs, old_revisions, revision=None):
    global count
    original_revision = row.get('revision')
    if revision is not None:
        row['revision'] = str(revision)
    row['_legacy_selection'] = {'policy_version': policy, 'chosen_table': chosen_table, 'reason': reason, 'source_records': refs, 'reported_revisions': old_revisions, 'original_selected_revision': None if original_revision is None else str(original_revision), 'snapshot': manifest['postgres']['snapshot'], 'source_manifest_id': manifest['id'], 'original_rows_retained': 'full checksummed source archives; row offsets resolve both immutable original rows', 'semantics': 'final_revision_history'}
    data = json.dumps(row, ensure_ascii=True, separators=(',', ':')).encode() + b'\n'
    output.write(data); h.update(data); count += 1
    selected[reason] += 1; states[row.get('state') or 'final_history_unknown_original_confirmation'] += 1

with candle_path.open('rb') as candles, (archive / tables['realtime_bars']['file']).open('rb') as realtime:
    for rt_offset, line in enumerate(realtime):
        row = json.loads(line, parse_float=str)
        found = connection.execute('SELECT byte_offset,row_offset FROM offsets WHERE k=?', (key(row),)).fetchone()
        if found is None:
            publish(row, 'realtime_bars', 'realtime_only', [ref('realtime_bars', rt_offset)], {'realtime_bars': str(row['revision'])})
            continue
        candles.seek(found[0]); candle = json.loads(candles.readline(), parse_float=str)
        refs = [ref('candles', found[1]), ref('realtime_bars', rt_offset)]
        revisions = {'candles': None if candle.get('revision') is None else str(candle['revision']), 'realtime_bars': str(row['revision'])}
        if row['state'] == 'final' and equal(candle, row) and ns(row['received_at']) == ns(candle['received_at']):
            end = ns(row['open_time']) + int(row['interval_seconds']) * 10**9
            if end > ns(row['received_at']) and row.get('finalized_at') is not None and ns(row['finalized_at']) < end:
                invalid_finality.append({'source_records': refs, 'instrument_symbol': row['instrument_symbol'], 'source_id': source(row), 'open_time': row['open_time'], 'interval_seconds': row['interval_seconds'], 'received_at': row['received_at'], 'original_finalized_at': row['finalized_at'], 'candles_history_file': (candle.get('raw_payload') or {}).get('history_file'), 'realtime_history_file': (row.get('source_raw_payload') or row.get('raw_payload') or {}).get('history_file'), 'candles_raw_bar_state': (candle.get('raw_payload') or {}).get('bar_state'), 'realtime_raw_bar_state': (row.get('source_raw_payload') or row.get('raw_payload') or {}).get('bar_state'), 'reason': 'invalid_legacy_finality: finalization precedes structural bucket end; no later same-key confirmation in fixed snapshot'})
                row['_invalid_legacy_finality'] = {'reason': invalid_finality[-1]['reason'], 'original_state': row['state'], 'original_finalized_at': row['finalized_at']}
                row['state'] = 'provisional_authoritative'
                row['finalized_at'] = None
            publish(row, 'realtime_bars', 'equivalent_final_keep_realtime_provenance', refs, revisions)
        elif row['state'] == 'provisional_quote' and not equal(candle, row) and ns(row['received_at']) < ns(candle['received_at']):
            authoritative_history(candle, True)
            if (candle.get('raw_payload') or {}).get('bar_state') != 'final':
                raise RuntimeError('preview replacement lacks explicit final bar_state')
            revision = max(int(candle.get('revision') or 1), int(row['revision']) + 1)
            if revision > 2**64 - 1:
                raise RuntimeError('canonical revision exceeds u64')
            publish(candle, 'candles', 'later_authoritative_final_replaces_preview', refs, revisions, revision)
        else:
            raise RuntimeError('unreviewed overlap category; selection stopped')
        connection.execute('UPDATE offsets SET selected=1 WHERE k=?', (key(row),))
        if rt_offset % 500 == 0:
            connection.commit(); budget()
    connection.commit()
    for byte_offset, row_offset in connection.execute('SELECT byte_offset,row_offset FROM offsets WHERE selected=0 ORDER BY byte_offset'):
        candles.seek(byte_offset); candle = json.loads(candles.readline(), parse_float=str)
        authoritative_history(candle, False)
        publish(candle, 'candles', 'authoritative_history_only', [ref('candles', row_offset)], {'candles': None if candle.get('revision') is None else str(candle['revision'])})
output.flush(); os.fsync(output.fileno()); output.close(); connection.close()
expected = int(tables['candles']['rows']) + int(tables['realtime_bars']['rows']) - selected['equivalent_final_keep_realtime_provenance'] - selected['later_authoritative_final_replaces_preview']
if count != expected:
    raise RuntimeError('canonical count differs from selected key union')
descriptor = {'schema': 1, 'policy_version': policy, 'source_manifest_id': manifest['id'], 'source_fingerprint': manifest['postgres']['fingerprint'], 'snapshot': manifest['postgres']['snapshot'], 'source_tables': {name: tables[name] for name in ['candles', 'realtime_bars']}, 'file': output_path.name, 'sha256': h.hexdigest(), 'rows': str(count), 'selection_counts': dict(selected), 'states': dict(states), 'invalid_legacy_finality_rows': invalid_finality, 'activation': False, 'raw_projection_cursor_inferred': False}
path = archive / 'canonical-bars-v1.manifest.json'
with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
    json.dump(descriptor, file, indent=2); file.flush(); os.fsync(file.fileno())
print(json.dumps({'rows': count, 'selection_counts': dict(selected), 'descriptor': str(path)}))
