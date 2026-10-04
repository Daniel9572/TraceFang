#!/usr/bin/env python3
"""Fixed archive only: exact bar overlap evidence, bounded disk key index."""
import collections, datetime, decimal, hashlib, json, pathlib, sqlite3, sys
archive, cache, report = map(pathlib.Path, sys.argv[1:4])
reuse = '--reuse-index' in sys.argv[4:]
cache.mkdir(parents=True, exist_ok=True)
database = cache / 'bar-keys.sqlite'
if database.exists():
    marker = cache / 'tracefang-owned-probe.json'
    if not marker.exists() or json.loads(marker.read_text()).get('purpose') != 'fixed-legacy-conflict-audit':
        raise SystemExit('existing cache is not an owned conflict audit')
    if not reuse:
        database.unlink()
(cache / 'tracefang-owned-probe.json').write_text('{"purpose":"fixed-legacy-conflict-audit"}')
manifest = json.loads((archive / 'manifest.json').read_text())
tables = {row['table']: row for row in manifest['tables']}

def hash_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def rows(name):
    entry = tables[name]
    path = archive / entry['file']
    if hash_file(path) != entry['sha256']:
        raise RuntimeError('fixed table checksum differs')
    with path.open() as f:
        for line in f:
            yield json.loads(line, parse_float=str)

def source(row):
    raw = row.get('realtime_source_id') or row.get('source_id')
    return 'jin10_client' if raw.startswith('jin10_') else raw

def ns(value):
    if value is None:
        return None
    try:
        date = datetime.datetime.fromisoformat(value).astimezone(datetime.timezone.utc)
    except ValueError:
        date = datetime.datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%f%z' if '.' in value else '%Y-%m-%dT%H:%M:%S%z').astimezone(datetime.timezone.utc)
    delta = date - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000

def key(row):
    return json.dumps([source(row), row['instrument_symbol'], row['interval_seconds'], ns(row['open_time'])], separators=(',', ':'))

def evidence(row, table):
    fields = ['instrument_symbol', 'source_id', 'realtime_source_id', 'upstream_channel_id', 'interval_seconds', 'open_time', 'revision', 'state', 'observed_at', 'received_at', 'finalized_at', 'open', 'high', 'low', 'close', 'volume']
    result = {field: row.get(field) for field in fields}
    for field in ['open', 'high', 'low', 'close', 'volume', 'revision']:
        if result[field] is not None:
            result[field] = str(result[field])
    result.update(table=table, logical_source=source(row), raw_bar_state=(row.get('raw_payload') or {}).get('bar_state'), history_file=(row.get('raw_payload') or {}).get('history_file'))
    return result

connection = sqlite3.connect(database)
connection.execute('PRAGMA journal_mode=OFF')
connection.execute('PRAGMA cache_size=-16384')
if not reuse:
    connection.execute('CREATE TABLE candles(k TEXT PRIMARY KEY, data TEXT) WITHOUT ROWID')
counts = {'candles': collections.Counter(), 'realtime_bars': collections.Counter()}
states = collections.Counter()
duplicates = 0
snapshot_ns = ns(manifest['postgres']['captured_at'])
candle_boundaries = collections.Counter()
candle_raw_states = collections.Counter()
boundary_samples = {}
for row in rows('candles'):
    counts['candles'][row['source_id']] += 1
    end_ns = ns(row['open_time']) + int(row['interval_seconds']) * 10**9
    candle_raw_states[(row.get('raw_payload') or {}).get('bar_state') or 'unknown'] += 1
    for name, boundary in [('snapshot', snapshot_ns), ('received_at', ns(row['received_at']))]:
        if end_ns > boundary:
            candle_boundaries['end_after_' + name] += 1
            boundary_samples.setdefault('end_after_' + name, evidence(row, 'candles'))
    if reuse:
        continue
    try:
        connection.execute('INSERT INTO candles VALUES (?,?)', (key(row), json.dumps(evidence(row, 'candles'))))
    except sqlite3.IntegrityError:
        duplicates += 1
connection.commit()
overlap = 0
categories = collections.Counter()
by_source = collections.Counter()
samples = {}
details = {}
for row in rows('realtime_bars'):
    counts['realtime_bars'][row['realtime_source_id']] += 1
    states[row['state']] += 1
    old = connection.execute('SELECT data FROM candles WHERE k=?', (key(row),)).fetchone()
    if old is None:
        continue
    candle = json.loads(old[0]); current = evidence(row, 'realtime_bars'); overlap += 1
    by_source[source(row)] += 1
    equal = all((a is None and b is None) or (a is not None and b is not None and decimal.Decimal(a) == decimal.Decimal(b)) for a, b in [(candle[k], current[k]) for k in ['open', 'high', 'low', 'close', 'volume']])
    changed = 'same_ohlcv' if equal else 'different_ohlcv'
    clock = 'realtime_received_older' if ns(current['received_at']) < ns(candle['received_at']) else 'realtime_received_same' if ns(current['received_at']) == ns(candle['received_at']) else 'realtime_received_newer'
    category = f'{current["state"]}/{changed}/{clock}'
    categories[category] += 1
    detail = details.setdefault(category, {'raw_bar_states': collections.Counter(), 'history_file_present': 0, 'source_counts': collections.Counter(), 'field_differences': collections.Counter(), 'end_after_snapshot': 0, 'end_after_received': 0, 'end_equal_received': 0, 'ohlc_changed': 0, 'volume_changed': 0})
    detail['raw_bar_states'][candle.get('raw_bar_state') or 'unknown'] += 1
    detail['history_file_present'] += int(bool(candle.get('history_file')))
    detail['source_counts'][candle['source_id']] += 1
    differences = [field for field in ['open', 'high', 'low', 'close', 'volume'] if not ((candle[field] is None and current[field] is None) or (candle[field] is not None and current[field] is not None and decimal.Decimal(candle[field]) == decimal.Decimal(current[field])))]
    detail['field_differences'].update(differences)
    detail['ohlc_changed'] += int(any(field != 'volume' for field in differences))
    detail['volume_changed'] += int('volume' in differences)
    end_ns = ns(candle['open_time']) + int(candle['interval_seconds']) * 10**9
    detail['end_after_snapshot'] += int(end_ns > snapshot_ns)
    detail['end_after_received'] += int(end_ns > ns(candle['received_at']))
    detail['end_equal_received'] += int(end_ns == ns(candle['received_at']))
    samples.setdefault(category, {'candles': candle, 'realtime_bars': current})
connection.close()
priority = sorted(samples, key=lambda value: ('different_ohlcv' not in value, 'realtime_received_older' not in value, value.startswith('final/'), value))
chosen = priority[:3]
result = {'source_manifest_id': manifest['id'], 'pg_snapshot': manifest['postgres']['snapshot'], 'snapshot_captured_at': manifest['postgres']['captured_at'], 'source_table_sha256': {name: tables[name]['sha256'] for name in counts}, 'table_source_counts': {name: dict(value) for name, value in counts.items()}, 'realtime_states': dict(states), 'candles_duplicate_logical_keys': duplicates, 'candles_raw_bar_states': dict(candle_raw_states), 'candles_end_boundaries': {'end_after_snapshot': 0, 'end_after_received_at': 0, **dict(candle_boundaries)}, 'boundary_samples': boundary_samples, 'overlapping_logical_keys': overlap, 'overlap_by_source': dict(by_source), 'categories': dict(categories), 'category_details': details, 'representative_samples': {name: samples[name] for name in chosen}, 'limits': ['fixed source archive, no mutable live PG read', 'all price equality uses arbitrary exact Decimal, never float', 'source timestamps preserve existing PG microseconds; unavailable finalization stays null', 'candles raw evidence remains retained; this report does not select precedence'], 'temporary_index_bytes': database.stat().st_size}
report.write_text(json.dumps(result, indent=2))
print(json.dumps({'overlapping_logical_keys': overlap, 'report': str(report)}))
