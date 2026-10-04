"""Small deterministic checks for terminal-v3 Python evidence guards; no services or Cargo."""
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


terminal = load('migration_terminal_handoff', 'scripts/migration-terminal-handoff.py')
preflight = load('migration_space_preflight', 'scripts/migration-space-preflight.py')
sealer = load('seal_migration_tools', 'scripts/seal-migration-tools.py')


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    return path


def _archive_fixture(root, group_id, objects, source_binding):
    """Write native per-file gzip receipts plus their source-domain adapter."""
    archive_directory = root / 'archives'
    archive_directory.mkdir(exist_ok=True)
    native_files = []
    adapter_entries = []
    total_compressed = total_source = total_allocated = 0
    for index, item in enumerate(objects):
        content = item['content']
        source_path = item['original_path']
        source_sha = hashlib.sha256(content).hexdigest()
        archive_path = archive_directory / (Path(source_path).name + '.gz')
        archive_path.write_bytes(gzip.compress(content, mtime=0))
        archive_stat = archive_path.stat()
        archive_size = archive_stat.st_size
        archive_allocated = getattr(archive_stat, 'st_blocks', 0) * 512 or archive_size
        mtime = 100 + index
        inode = 20 + index
        source_identity = {'device': 10, 'inode': inode, 'size': len(content),
                           'mtime_ns': mtime, 'ctime_ns': mtime, 'flags': 0}
        source_metadata = {
            'path': source_path, 'identity': source_identity,
            'allocated_bytes': max(1, len(content)), 'mode': 384,
            'full_mode_octal': '0o100600', 'uid': 501, 'gid': 20,
            'mtime_ns': mtime, 'atime_ns': mtime, 'ctime_ns': mtime,
            'birthtime_ns': mtime, 'flags': 0, 'nlink': len(item['aliases']),
            'xattrs': {}, 'acl_flags_listing': 'regular file',
        }
        native_files.append({
            'source_name': Path(source_path).name, 'source_path': source_path,
            'source_sha256': source_sha, 'source_bytes': len(content),
            'source_metadata_before': source_metadata, 'source_metadata_after': source_metadata,
            'archive_file': str(archive_path), 'archive_sha256': preflight.sha(archive_path),
            'compressed_size': archive_size, 'archive_allocated_bytes': archive_allocated,
            'status': 'verified', 'file_fsync_succeeded': True,
            'archive_directory_fsync_succeeded': True, 'gzip_crc_eof_verified': True,
            'gzip_handle_closed': True, 'output_fd_closed': True, 'source_fd_closed': True,
            'independent_decompressed_bytes': len(content),
            'independent_decompressed_sha256': source_sha,
        })
        adapter_entries.append({'source_path': source_path, 'kind': item['kind'],
                                'aliases': item['aliases']})
        total_compressed += archive_size
        total_source += len(content)
        total_allocated += archive_allocated
    restore_path = root / (group_id + '.restore-manifest.json')
    floor = 4 * 1024**3
    scratch = 1024**2
    if len(native_files) == 1:
        # Match the old-7051 producer's root-level single-file receipt exactly.
        native = native_files[0]
        restore = {
            'schema': 'tracefang-restore-manifest-v1', 'status': 'complete',
            'group_id': group_id, 'archive_id': 'fixture-archive-id',
            'archive_directory': str(archive_directory),
            'source_path': native['source_path'], 'source_sha256': native['source_sha256'],
            'source_bytes': native['source_bytes'],
            'source_metadata_before': native['source_metadata_before'],
            'source_metadata_after': native['source_metadata_after'],
            'archive_file': native['archive_file'], 'archive_sha256': native['archive_sha256'],
            'compressed_size': native['compressed_size'],
            'archive_allocated_bytes': native['archive_allocated_bytes'],
            'file_fsync_succeeded': True, 'archive_directory_fsync_succeeded': True,
            'archive_fd_closed': True, 'source_fd_closed': True,
            'gzip_handle_closed': True, 'gzip_crc_eof_verified': True,
            'restore_manifest_path': str(restore_path.resolve()),
            'atime_before_ns': native['source_metadata_before']['atime_ns'],
            'atime_after_ns': native['source_metadata_after']['atime_ns'],
            'independent_decompressed_bytes': native['independent_decompressed_bytes'],
            'independent_decompressed_sha256': native['independent_decompressed_sha256'],
            'logical_source_bytes': total_source, 'minimum_free_bytes': floor,
            'scratch_disk_bytes': 0, 'archive_credit_bytes': 0,
            'space_released_bytes': 0,
            'durable_location': True, 'source_identity_preserved': True,
            'source_metadata_preserved_except_possible_atime': True,
            'restore_manifest_fsync_succeeded': True,
            'release': {'archive_credit_bytes': 0, 'originals_deleted_or_moved': False,
                        'space_released_bytes': 0},
            'restore_steps': ['verify archive SHA', 'stream-decompress and verify complete source SHA'],
        }
    else:
        restore = {
            'schema': 'tracefang-restore-manifest-v1', 'status': 'complete',
            'group_id': group_id, 'source_directory': str(Path(objects[0]['original_path']).parent),
            'archive_directory': str(archive_directory), 'files': native_files,
            'all_source_output_and_gzip_handles_closed': True,
            'archive_credit_bytes': 0, 'archive_allocated_bytes_total': total_allocated,
            'compressed_bytes_total': total_compressed, 'logical_source_bytes': total_source,
            'durable_location': True, 'source_identity_preserved': True,
            'source_metadata_preserved_except_possible_atime': True,
            'output_directory_fsync_succeeded': True, 'restore_manifest_fsync_succeeded': True,
            'minimum_free_bytes': floor, 'scratch_disk_bytes': 0,
            'release': {'archive_credit_bytes': 0, 'originals_deleted_or_moved': False,
                        'space_released_bytes': 0},
            'restore_steps': ['verify archive SHA', 'stream-decompress and verify complete source SHA'],
        }
    _write_json(restore_path, restore)
    return {
        'group_id': group_id,
        'restore_manifest': {'file': str(restore_path), 'sha256': preflight.sha(restore_path)},
        'source_binding': source_binding,
        'recovery': {'scratch_limit_bytes': str(scratch), 'free_floor_bytes': str(floor),
                     'restore_required_free_bytes': str(total_source + scratch + floor)},
        'entries': adapter_entries,
        'restore_path': restore_path,
        'archive_path': Path(native_files[0]['archive_file']),
    }


def _complete_archive_fixture(root):
    source = root / 'source'
    source.mkdir()

    def retained(name, payload):
        path = source / name
        path.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    config_sha = retained('config.toml', b'config = true\n')
    raw_sha = retained('raw.frames', b'retained raw envelope\n')
    mapping_sha = retained('mapping.json', b'{"mapping":true}\n')
    table_bytes = b'old postgres table bytes\n'
    canonical_bytes = b'old canonical bytes\n'
    facts_bytes = b'old native facts bytes\n'
    table_path = source / 'table.dump'
    canonical_path = source / 'canonical.ndjson'
    facts_path = root / 'legacy' / 'facts.redb'
    manifest = {
        'schema': 1, 'id': 'fixture-pg-snapshot', 'postgres': {'snapshot': 'pg-snapshot-1'},
        'configs': [{'file': 'config.toml', 'sha256': config_sha}],
        'tables': [{'table': 'legacy_bars', 'file': table_path.name, 'rows': '1',
                    'sha256': hashlib.sha256(table_bytes).hexdigest()}],
        'raw': {'file': 'raw.frames', 'sha256': raw_sha,
                'native_mapping': {'file': 'mapping.json', 'sha256': mapping_sha}},
    }
    _write_json(source / 'manifest.json', manifest)
    canonical_desc = {'schema': 1, 'source_manifest_id': 'fixture-pg-snapshot',
                      'snapshot': 'pg-snapshot-1', 'file': canonical_path.name,
                      'sha256': hashlib.sha256(canonical_bytes).hexdigest(), 'source_tables': {}}
    _write_json(source / 'canonical-bars-v1.manifest.json', canonical_desc)
    source_ref = {'file': str((source / 'manifest.json').resolve()),
                  'sha256': preflight.sha(source / 'manifest.json')}
    source_binding = {'source_manifest': source_ref,
        'source_manifest_id': 'fixture-pg-snapshot', 'postgres_snapshot': 'pg-snapshot-1'}
    descriptor_ref = {'file': str((source / 'canonical-bars-v1.manifest.json').resolve()),
                      'sha256': preflight.sha(source / 'canonical-bars-v1.manifest.json')}
    table_group = _archive_fixture(root, 'retired-original-pg-table-pairs', [
        {'original_path': str(table_path), 'content': table_bytes, 'kind': 'postgres_table',
         'aliases': [str(table_path)]}], source_binding)
    canonical_group = _archive_fixture(root, 'retired-original-canonical', [
        {'original_path': str(canonical_path), 'content': canonical_bytes, 'kind': 'canonical',
         'aliases': [str(canonical_path)]}], {**source_binding, 'canonical_descriptor': descriptor_ref})
    verification_report_path = root / 'native-facts-verification.json'
    reopen_report_path = root / 'native-facts-reopen.json'
    native_binding = {'source_path': str(facts_path), 'generation': 'legacy-fixture-pg-snapshot',
        'commit_id': '17', 'fact_codec_sha256': 'a' * 64, 'index_codec_sha256': 'b' * 64,
        'store_epoch': 'fixture-store-epoch', 'schema_version': 'tracefang-native-v1',
        'aggregation_version': 'fixture-aggregation', 'fact_rows': '12', 'index_nodes': '3',
        'verification_report': {'file': str(verification_report_path)}}
    _write_json(verification_report_path, {'schema': 'fixture-native-verification-v1', 'complete': True,
        'proof': {'complete': True, 'index_verified': True,
            'generation': 'legacy-fixture-pg-snapshot', 'verified_commit_id': '17',
            'store_epoch': 'fixture-store-epoch', 'schema_version': 'tracefang-native-v1',
            'aggregation_version': 'fixture-aggregation', 'fact_rows': '12', 'index_nodes': '3',
            'fact_codec_sha256': 'a' * 64, 'index_codec_sha256': 'b' * 64}})
    _write_json(reopen_report_path, {'schema': 'fixture-native-reopen-v1',
        'active_version': {'store_epoch': 'fixture-store-epoch'},
        'verified': {'complete': True, 'index_verified': True,
                     'schema_version': 'tracefang-native-v1',
                     'aggregation_version': 'fixture-aggregation', 'fact_rows': '12',
                     'index_nodes': '3', 'fact_codec_sha256': 'a' * 64,
                     'index_codec_sha256': 'b' * 64}})
    native_binding['verification_report']['sha256'] = preflight.sha(verification_report_path)
    native_binding['reopen_report'] = {'file': str(reopen_report_path),
                                       'sha256': preflight.sha(reopen_report_path)}
    facts_group = _archive_fixture(root, 'old-7051-facts', [
        {'original_path': str(facts_path), 'content': facts_bytes, 'kind': 'facts',
         'aliases': [str(facts_path)]}], {**source_binding, 'native_facts': native_binding})
    receipt_path = root / 'archive-receipt.json'
    _write_json(receipt_path, {'schema': 'tracefang-archived-input-adapter-v1',
        'complete': True, 'archive_credit_bytes': '0', 'originals_deleted_or_moved': False,
        'source_of_stream_and_metadata_claims':
            'immutable producer restore_manifest only; adapter does not add or override these claims',
        'groups': [{key: value for key, value in group.items() if key not in ('restore_path', 'archive_path')}
                   for group in (table_group, canonical_group, facts_group)]})
    return {'source': source, 'facts': facts_path, 'receipt': receipt_path,
            'table_archive': table_group['archive_path'],
            'table_restore': table_group['restore_path']}


def _refresh_receipt_after_restore_edit(fixture, mutate):
    restore_path = fixture['table_restore']
    receipt_path = fixture['receipt']
    restore = json.loads(restore_path.read_text())
    mutate(restore)
    _write_json(restore_path, restore)
    receipt = json.loads(receipt_path.read_text())
    for group in receipt['groups']:
        if group['group_id'] == 'retired-original-pg-table-pairs':
            group['restore_manifest']['sha256'] = preflight.sha(restore_path)
    _write_json(receipt_path, receipt)


def _alias_archive_fixture(root):
    """Use the actual entries/by-alias producer field names for PG and canonical."""
    fixture = _complete_archive_fixture(root)
    receipt = json.loads(fixture['receipt'].read_text())
    corrected = root / 'corrected'
    corrected.mkdir()
    original = fixture['source'] / 'manifest.json'
    _write_json(corrected / 'manifest.json', json.loads(original.read_text()))

    def source_ref(path, role):
        st = path.stat()
        identity = {'device': st.st_dev, 'inode': st.st_ino, 'size': st.st_size,
                    'mtime_ns': st.st_mtime_ns, 'ctime_ns': st.st_ctime_ns, 'flags': 0}
        metadata = {'path': str(path), 'identity': identity, 'allocated_bytes': st.st_blocks * 512,
                    'mode': 384, 'full_mode_octal': '0o100600', 'uid': st.st_uid,
                    'gid': st.st_gid, 'mtime_ns': st.st_mtime_ns, 'atime_ns': st.st_atime_ns,
                    'ctime_ns': st.st_ctime_ns, 'birthtime_ns': 0, 'flags': 0,
                    'nlink': 1, 'xattrs': {}, 'acl_flags_listing': 'regular file'}
        return {'path': str(path), 'role': role, 'sha256': preflight.sha(path),
                'bytes': st.st_size, 'metadata_before': metadata, 'metadata_after_read': metadata}

    refs = [source_ref(original, 'original_source_manifest'),
            source_ref(corrected / 'manifest.json', 'corrected_sources_progress_manifest'),
            source_ref(fixture['source'] / 'canonical-bars-v1.manifest.json',
                       'original_canonical_descriptor')]
    after_refs = json.loads(json.dumps(refs))
    refs[2].update(activation=False, declared_content_sha256='fixture descriptor content claim')
    for group in receipt['groups'][:2]:
        path = Path(group['restore_manifest']['file'])
        old = json.loads(path.read_text())
        primary = old['source_path']
        aliases = [primary]
        if group['group_id'] == 'retired-original-pg-table-pairs':
            aliases.append(str(corrected / Path(primary).name))
        metadata = []
        for alias in aliases:
            item = json.loads(json.dumps(old['source_metadata_before']))
            item.update(path=alias, nlink=len(aliases))
            metadata.append(item)
        entry = {'primary_alias': primary, 'alias_paths': aliases, 'alias_count': len(aliases),
                 'expected_nlink': len(aliases), 'logical_bytes': old['source_bytes'],
                 'source_sha256': old['source_sha256'],
                 'expected_recorded_sha256': old['source_sha256'],
                 'source_inode_identity': old['source_metadata_before']['identity'],
                 'source_metadata_before_by_alias': metadata,
                 'source_metadata_after_by_alias': json.loads(json.dumps(metadata)),
                 'archive_size': old['compressed_size'],
                 **{key: old[key] for key in ('archive_file', 'archive_sha256',
                      'archive_allocated_bytes', 'file_fsync_succeeded',
                      'archive_directory_fsync_succeeded', 'archive_fd_closed', 'source_fd_closed',
                      'gzip_handle_closed', 'gzip_crc_eof_verified',
                      'independent_decompressed_bytes', 'independent_decompressed_sha256')}}
        native = {key: old[key] for key in ('schema', 'status', 'group_id', 'archive_directory',
                  'durable_location', 'source_identity_preserved', 'release', 'archive_credit_bytes',
                  'space_released_bytes', 'minimum_free_bytes', 'scratch_disk_bytes',
                  'restore_manifest_fsync_succeeded', 'restore_manifest_path')}
        native.update(entries=[entry], all_known_nlinks_accounted_for=True,
                      all_source_aliases_retained=True, unique_source_inode_count=1,
                      source_alias_paths_count=len(aliases), source_metadata_after_all_files=metadata,
                      source_manifest_and_descriptor_sha256=refs,
                      source_manifest_and_descriptor_metadata_after=after_refs,
                      archive_compressed_bytes_total=old['compressed_size'],
                      archive_allocated_bytes_total=old['archive_allocated_bytes'],
                      logical_source_bytes_unique=old['source_bytes'])
        _write_json(path, native)
        group['restore_manifest']['sha256'] = preflight.sha(path)
        group['entries'][0]['aliases'] = aliases
    _write_json(fixture['receipt'], receipt)
    fixture['corrected_alias'] = str((corrected / 'table.dump').resolve())
    return fixture


class ShortReader:
    def __init__(self, source):
        self.source = source

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.source.close()

    def fileno(self):
        return self.source.fileno()

    def read(self, _size=-1):
        return b''


class TerminalV3Guards(unittest.TestCase):
    def test_stable_read_rejects_short_read_even_when_stat_is_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'nonempty.json'
            path.write_bytes(b'{"nonempty":true}')
            original_open = Path.open

            def short_open(candidate, *args, **kwargs):
                stream = original_open(candidate, *args, **kwargs)
                return ShortReader(stream) if candidate == path else stream

            with mock.patch.object(Path, 'open', new=short_open):
                with self.assertRaisesRegex(ValueError, 'short-read'):
                    terminal.stable_read(path)

    def test_stable_hash_rejects_file_identity_change_during_read(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'input.bin'
            path.write_bytes(b'complete bytes')
            actual = os.fstat
            calls = 0

            def changed_fstat(fd):
                nonlocal calls
                calls += 1
                value = actual(fd)
                if calls == 2:
                    return types.SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino,
                                                 st_size=value.st_size, st_mtime_ns=value.st_mtime_ns + 1)
                return value

            with mock.patch.object(terminal.os, 'fstat', side_effect=changed_fstat):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    terminal.sha(path)

    def test_string_executable_is_resolved_and_rehashed(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / 'sealed-helper'
            executable.write_bytes(b'helper-v1')
            executable.chmod(0o500)
            sealed = {executable.resolve(): hashlib.sha256(b'helper-v1').hexdigest()}
            terminal.verify_execution([str(executable), 'identity'], sealed)
            executable.chmod(0o600)
            executable.write_bytes(b'helper-v2')
            with self.assertRaisesRegex(ValueError, 'unsealed/changed executable'):
                terminal.verify_execution([str(executable), 'identity'], sealed)

    def test_python_script_and_runtime_are_both_sealed(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / 'input.py'
            script.write_text('pass\n')
            runtime = Path(os.path.realpath(os.sys.executable))
            sealed = {runtime: terminal.sha(runtime), script.resolve(): terminal.sha(script)}
            terminal.verify_execution([os.sys.executable, str(script)], sealed)
            script.write_text('raise SystemExit(0)\n')
            with self.assertRaisesRegex(ValueError, 'unsealed/changed Python script'):
                terminal.verify_execution([os.sys.executable, str(script)], sealed)

    def test_gzip_ledger_reads_every_member_and_checks_row_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'ledger.ndjson.gz'
            path.write_bytes(gzip.compress(b'{"part":1}\n') + gzip.compress(b'{"part":2}\n'))
            ref = {'file': path.name, 'sha256': terminal.sha(path), 'complete': True, 'row_count': '2'}
            self.assertEqual(terminal.verify_report_artifact(root, ref), path.resolve())
            ref['row_count'] = '1'
            with self.assertRaisesRegex(ValueError, 'row count'):
                terminal.verify_report_artifact(root, ref)

    def test_gzip_ledger_rejects_bad_crc_and_trailing_garbage(self):
        valid = gzip.compress(b'{"part":1}\n')
        for suffix, payload in (('crc', valid[:-2]), ('garbage', valid + b'not-a-member')):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path = root / 'ledger.ndjson.gz'
                path.write_bytes(payload)
                ref = {'file': path.name, 'sha256': terminal.sha(path), 'complete': True, 'row_count': '1'}
                with self.assertRaises((OSError, EOFError, ValueError)):
                    terminal.verify_report_artifact(root, ref)

    def test_space_gate_never_credits_archive_savings(self):
        plan = preflight.assess(1000, 10, 20, 30, 40, target_cap=100,
                                reserve=100, phase='retained_reconciliation',
                                clock_output_cap=20, spool_cap=30, scope_cap=40,
                                scratch_cap=50, evidence_cap=10)
        self.assertEqual(plan['archive_credit_bytes'], '0')
        self.assertTrue(plan['future_phase_requires_fresh_free_and_size_gate'])

    def test_archive_resolution_requires_exact_named_group_and_sha(self):
        expected = 'a' * 64
        receipt = {'files': {'/old/facts.redb': {
            'group_id': 'old-7051-facts', 'sha256': expected, 'bytes': 10,
            'allocated_bytes': 12, 'archive': {}, 'archive_summary': {}, 'restore_manifest': {}}}}
        self.assertEqual(preflight.archived_file(receipt, '/old/facts.redb', 'facts', expected)['bytes'], 10)
        self.assertIsNone(preflight.archived_file(receipt, '/old/facts.redb', 'canonical', expected))
        with self.assertRaisesRegex(ValueError, 'SHA differs'):
            preflight.archived_file(receipt, '/old/facts.redb', 'facts', 'b' * 64)

    def test_actual_alias_producer_shape_preserves_both_pg_aliases_without_decompression(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = _alias_archive_fixture(Path(temp))
            with mock.patch.object(preflight, '_verify_gzip_stream',
                                   side_effect=AssertionError('unexpected full decompression')):
                receipt = preflight.validate_archive_receipt(fixture['receipt'])
            self.assertEqual(len(receipt['files']), 4)
            self.assertIn(fixture['corrected_alias'], receipt['files'])
            self.assertEqual(receipt['files'][fixture['corrected_alias']]['kind'], 'postgres_table')

    def test_actual_alias_producer_rejects_missing_or_changed_secondary_alias_metadata(self):
        mutations = [
            lambda x: x['entries'][0]['source_metadata_before_by_alias'].pop(),
            lambda x: x['entries'][0]['source_metadata_after_by_alias'][1]['identity'].update(inode=99),
            lambda x: x['source_metadata_after_all_files'].pop(),
            lambda x: x['entries'][0].update(alias_count=1),
            lambda x: x['entries'][0].update(archive_fd_closed=False),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp:
                fixture = _alias_archive_fixture(Path(temp))
                _refresh_receipt_after_restore_edit(fixture, mutate)
                with self.assertRaises(ValueError):
                    preflight.validate_archive_receipt(fixture['receipt'])

    def test_current_identity_rebind_requires_full_content_and_all_alias_proofs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = _alias_archive_fixture(root)
            _refresh_receipt_after_restore_edit(fixture, lambda x: (
                x['entries'][0].update(archive_allocated_bytes=8192),
                x.update(archive_allocated_bytes_total=8192)))
            with self.assertRaisesRegex(ValueError, 'allocation differs'):
                preflight.validate_archive_receipt(fixture['receipt'])
            receipt = json.loads(fixture['receipt'].read_text())
            group = receipt['groups'][0]
            native = json.loads(fixture['table_restore'].read_text())['entries'][0]
            st = fixture['table_archive'].stat()
            archive_metadata = {'identity': {'device': st.st_dev, 'inode': st.st_ino,
                'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'ctime_ns': st.st_ctime_ns}}
            record = {'source_path': native['primary_alias'], 'aliases': native['alias_paths'],
                'source_sha256': native['source_sha256'], 'source_bytes': native['logical_bytes'],
                'full_source_bytes_rehashed': native['logical_bytes'],
                'full_source_sha256_read_complete': True, 'current_aliases_same_inode_and_device': True,
                'all_source_handles_closed': True,
                'source_metadata_before_by_alias': native['source_metadata_before_by_alias'],
                'source_metadata_after_by_alias': native['source_metadata_after_by_alias'],
                'archive': {'file': native['archive_file'], 'sha256': native['archive_sha256'],
                    'bytes': native['archive_size'], 'allocated_bytes': st.st_blocks * 512,
                    'metadata_before': archive_metadata, 'metadata_after': archive_metadata,
                    'full_compressed_sha256_read_complete': True, 'archive_handle_closed': True}}
            proof = {'schema': 'tracefang-archive-current-identity-rebind-v1', 'complete': True,
                'all_read_handles_closed': True,
                'current_source_content_matches_producer_and_independent_decode': True,
                'all_current_source_alias_metadata_stable': True,
                'producer_restore_manifests_modified': False, 'archive_credit_bytes': '0',
                'groups': [{'group_id': group['group_id'],
                            'restore_manifest': group['restore_manifest'], 'entries': [record]}]}
            proof_path = root / 'current-rebind.json'
            mutations = [None, lambda p: p['groups'][0]['entries'][0].update(full_source_bytes_rehashed=0),
                lambda p: p['groups'][0]['entries'][0]['source_metadata_after_by_alias'].pop(),
                lambda p: p['groups'][0]['entries'][0]['archive'].update(allocated_bytes=1),
                lambda p: p['groups'][0]['restore_manifest'].update(sha256='f' * 64)]
            for index, mutate in enumerate(mutations):
                changed = json.loads(json.dumps(proof))
                if mutate:
                    mutate(changed)
                _write_json(proof_path, changed)
                group['current_identity_rebind'] = {'file': str(proof_path),
                                                  'sha256': preflight.sha(proof_path)}
                _write_json(fixture['receipt'], receipt)
                with self.subTest(index=index):
                    if mutate:
                        with self.assertRaises(ValueError):
                            preflight.validate_archive_receipt(fixture['receipt'])
                    else:
                        checked = preflight.validate_archive_receipt(fixture['receipt'])
                        self.assertEqual(checked['groups'][0]['current_identity_rebind']['sha256'],
                                         preflight.sha(proof_path))

    def test_progress_phase_rebind_keeps_exact_original_source_table_mapping(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = _alias_archive_fixture(root)
            native = json.loads(fixture['table_restore'].read_text())
            ref = next(item for item in native['source_manifest_and_descriptor_sha256']
                       if item['role'] == 'corrected_sources_progress_manifest')
            progress = json.loads(Path(ref['path']).read_text())
            progress['phases'] = {'metadata_repair': {'commit': '18', 'complete': True}}
            frozen = root / 'frozen-current-progress.json'
            _write_json(frozen, progress)
            override = {'role': ref['role'], 'historical_path': ref['path'],
                        'historical_sha256': ref['sha256'],
                        'verified_current_copy': {'file': str(frozen), 'sha256': preflight.sha(frozen)}}
            result = preflight._normalize_alias_restore(native, fixture['table_restore'].resolve(),
                        'retired-original-pg-table-pairs', [override])
            self.assertEqual(len(result['files']), 1)
            progress['tables'][0]['sha256'] = 'e' * 64
            _write_json(frozen, progress)
            override['verified_current_copy']['sha256'] = preflight.sha(frozen)
            with self.assertRaisesRegex(ValueError, 'alias body is absent'):
                preflight._normalize_alias_restore(native, fixture['table_restore'].resolve(),
                            'retired-original-pg-table-pairs', [override])

    def test_complete_archive_receipt_admits_only_named_absent_base_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = _complete_archive_fixture(root)
            with mock.patch.object(preflight, '_verify_gzip_stream',
                                   side_effect=AssertionError('phase validation re-decompressed archive')):
                receipt = preflight.validate_archive_receipt(fixture['receipt'])
            deep_audit = preflight.validate_archive_receipt(fixture['receipt'], verify_streams=True)
            self.assertEqual(deep_audit['files'], receipt['files'])
            facts_version = next(group['source_binding']['native_facts']
                                 for group in receipt['groups']
                                 if group['group_id'] == 'old-7051-facts')
            self.assertEqual(facts_version['store_epoch'], 'fixture-store-epoch')
            self.assertEqual(facts_version['schema_version'], 'tracefang-native-v1')
            self.assertEqual(facts_version['aggregation_version'], 'fixture-aggregation')
            self.assertEqual(facts_version['fact_rows'], '12')
            self.assertEqual(facts_version['index_nodes'], '3')
            source_manifest, pins = terminal.verify_source_snapshot(
                fixture['source'], receipt, allow_archived=True)
            self.assertEqual(source_manifest['id'], 'fixture-pg-snapshot')
            self.assertNotIn((fixture['source'] / 'table.dump').resolve(), pins)
            self.assertNotIn((fixture['source'] / 'canonical.ndjson').resolve(), pins)
            self.assertIn((fixture['source'] / 'raw.frames').resolve(), pins)
            self.assertIn((fixture['source'] / 'mapping.json').resolve(), pins)

            report_path = root / 'base-preflight.json'
            argv = ['--source-directory', str(fixture['source']), '--facts', str(fixture['facts']),
                    '--facts-kind', 'base', '--allow-archived-base-source',
                    '--archived-input-receipt', str(fixture['receipt']),
                    '--target-volume', str(root), '--phase', 'final_inputs',
                    '--raw-delta-budget-mib', '0', '--report', str(report_path)]
            with mock.patch.object(preflight.shutil, 'disk_usage',
                                   return_value=types.SimpleNamespace(free=100 * 1024**3)):
                self.assertEqual(preflight.main(argv), 0)
            report = json.loads(report_path.read_text())
            self.assertTrue(report['facts_input']['archived'])
            self.assertEqual(report['facts_input']['archive_group_id'], 'old-7051-facts')
            self.assertTrue(report['postgres_table_files'][0]['archived'])
            self.assertEqual(report['postgres_table_files'][0]['archive_group_id'],
                             'retired-original-pg-table-pairs')
            self.assertTrue(report['observed_canonical_source_bytes'] != '0')
            self.assertEqual(report['space_plan']['archive_credit_bytes'], '0')
            self.assertTrue(all(not item['archived'] for item in report['retained_raw_source_inputs']))

            for name in ('config.toml', 'raw.frames', 'mapping.json'):
                path = fixture['source'] / name
                expected = hashlib.sha256(path.read_bytes()).hexdigest()
                path.unlink()
                with self.subTest(missing=name), self.assertRaises(FileNotFoundError):
                    preflight.input_file(path, name, receipt, expected_sha=expected)
            facts_digest = next(item['sha256'] for item in receipt['files'].values()
                                if item['kind'] == 'facts')
            with self.assertRaises(FileNotFoundError):
                preflight.input_file(fixture['facts'], 'fresh facts', receipt,
                                     expected_sha=facts_digest)

    def test_archived_old_facts_do_not_bind_fresh_online_pg_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = _complete_archive_fixture(root)
            # A fresh PG export is online; only its older native facts remain
            # archived and carry their own exact historical source snapshot.
            (fixture['source'] / 'table.dump').write_bytes(b'old postgres table bytes\n')
            (fixture['source'] / 'canonical.ndjson').write_bytes(b'old canonical bytes\n')
            historical = root / 'old-facts-source' / 'manifest.json'
            _write_json(historical, {'schema': 1, 'id': 'old-facts-pg-source',
                                     'postgres': {'snapshot': 'old-facts-snapshot'}})
            receipt_doc = json.loads(fixture['receipt'].read_text())
            facts_group = next(group for group in receipt_doc['groups']
                               if group['group_id'] == 'old-7051-facts')
            facts_group['source_binding']['source_manifest'] = {
                'file': str(historical.resolve()), 'sha256': preflight.sha(historical)}
            facts_group['source_binding']['source_manifest_id'] = 'old-facts-pg-source'
            facts_group['source_binding']['postgres_snapshot'] = 'old-facts-snapshot'
            native = facts_group['source_binding']['native_facts']
            native['generation'] = 'legacy-old-facts-pg-source'
            verify_path = Path(native['verification_report']['file'])
            verify_doc = json.loads(verify_path.read_text())
            verify_doc['proof']['generation'] = native['generation']
            _write_json(verify_path, verify_doc)
            native['verification_report']['sha256'] = preflight.sha(verify_path)
            _write_json(fixture['receipt'], receipt_doc)

            terminal_receipt = preflight.validate_archive_receipt(fixture['receipt'])
            terminal.verify_source_snapshot(fixture['source'], terminal_receipt, allow_archived=True)

            report_path = root / 'fresh-pg-preflight.json'
            argv = ['--source-directory', str(fixture['source']), '--facts', str(fixture['facts']),
                    '--facts-kind', 'base', '--archived-input-receipt', str(fixture['receipt']),
                    '--target-volume', str(root), '--phase', 'final_inputs',
                    '--raw-delta-budget-mib', '0', '--report', str(report_path)]
            with mock.patch.object(preflight.shutil, 'disk_usage',
                                   return_value=types.SimpleNamespace(free=100 * 1024**3)):
                self.assertEqual(preflight.main(argv), 0)
            report = json.loads(report_path.read_text())
            self.assertEqual(report['source_manifest_id'], 'fixture-pg-snapshot')
            self.assertTrue(report['facts_input']['archived'])
            self.assertFalse(any(item['archived'] for item in report['postgres_table_files']))
            self.assertGreater(int(report['observed_canonical_source_bytes']), 0)

    def test_complete_archive_receipt_rejects_bad_archive_alias_and_stream_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = _complete_archive_fixture(Path(temp))
            fixture['table_archive'].write_bytes(fixture['table_archive'].read_bytes() + b'changed')
            with self.assertRaisesRegex(ValueError, 'bytes do not match their SHA'):
                preflight.validate_archive_receipt(fixture['receipt'])

        with tempfile.TemporaryDirectory() as temp:
            fixture = _complete_archive_fixture(Path(temp))
            _refresh_receipt_after_restore_edit(fixture,
                lambda value: value['source_metadata_before'].update(nlink=2))
            with self.assertRaisesRegex(ValueError, 'alias inventory'):
                preflight.validate_archive_receipt(fixture['receipt'])

        with tempfile.TemporaryDirectory() as temp:
            fixture = _complete_archive_fixture(Path(temp))
            _refresh_receipt_after_restore_edit(fixture,
                lambda value: value.update(independent_decompressed_bytes=0))
            with self.assertRaisesRegex(ValueError, 'independent stream proof'):
                preflight.validate_archive_receipt(fixture['receipt'])

        with tempfile.TemporaryDirectory() as temp:
            fixture = _complete_archive_fixture(Path(temp))
            _refresh_receipt_after_restore_edit(fixture,
                lambda value: value['source_metadata_after'].update(mode=420))
            with self.assertRaisesRegex(ValueError, 'metadata identity is not stable'):
                preflight.validate_archive_receipt(fixture['receipt'])

        with tempfile.TemporaryDirectory() as temp:
            fixture = _complete_archive_fixture(Path(temp))
            def poison_xattr(value):
                record = {'base64': 'YQ==', 'length': 1, 'sha256': '0' * 64}
                value['source_metadata_before']['xattrs'] = {'com.test': record}
                value['source_metadata_after']['xattrs'] = {'com.test': record}
            _refresh_receipt_after_restore_edit(fixture, poison_xattr)
            with self.assertRaisesRegex(ValueError, 'xattr bytes do not match'):
                preflight.validate_archive_receipt(fixture['receipt'])

        with tempfile.TemporaryDirectory() as temp:
            fixture = _complete_archive_fixture(Path(temp))
            receipt_doc = json.loads(fixture['receipt'].read_text())
            facts_group = next(group for group in receipt_doc['groups']
                               if group['group_id'] == 'old-7051-facts')
            facts_group['source_binding']['native_facts']['generation'] = 'legacy-wrong-source-id'
            _write_json(fixture['receipt'], receipt_doc)
            with self.assertRaisesRegex(ValueError, 'exact native facts source/version'):
                preflight.validate_archive_receipt(fixture['receipt'])

        with tempfile.TemporaryDirectory() as temp:
            fixture = _complete_archive_fixture(Path(temp))
            receipt_doc = json.loads(fixture['receipt'].read_text())
            facts_group = next(group for group in receipt_doc['groups']
                               if group['group_id'] == 'old-7051-facts')
            native = facts_group['source_binding']['native_facts']
            verification_path = Path(native['verification_report']['file'])
            verification = json.loads(verification_path.read_text())
            verification['proof']['index_verified'] = False
            _write_json(verification_path, verification)
            native['verification_report']['sha256'] = preflight.sha(verification_path)
            _write_json(fixture['receipt'], receipt_doc)
            with self.assertRaisesRegex(ValueError, 'does not prove the bound native version/counts'):
                preflight.validate_archive_receipt(fixture['receipt'])

        with tempfile.TemporaryDirectory() as temp:
            fixture = _complete_archive_fixture(Path(temp))
            receipt_doc = json.loads(fixture['receipt'].read_text())
            facts_group = next(group for group in receipt_doc['groups']
                               if group['group_id'] == 'old-7051-facts')
            native = facts_group['source_binding']['native_facts']
            reopen_path = Path(native['reopen_report']['file'])
            reopen = json.loads(reopen_path.read_text())
            reopen['active_version']['store_epoch'] = 'wrong-epoch'
            _write_json(reopen_path, reopen)
            native['reopen_report']['sha256'] = preflight.sha(reopen_path)
            _write_json(fixture['receipt'], receipt_doc)
            with self.assertRaisesRegex(ValueError, 'independent reopen report does not match'):
                preflight.validate_archive_receipt(fixture['receipt'])

        with tempfile.TemporaryDirectory() as temp:
            fixture = _complete_archive_fixture(Path(temp))
            receipt_doc = json.loads(fixture['receipt'].read_text())
            facts_group = next(group for group in receipt_doc['groups']
                               if group['group_id'] == 'old-7051-facts')
            native = facts_group['source_binding']['native_facts']
            verify_path = Path(native['verification_report']['file'])
            verify = json.loads(verify_path.read_text())
            verify['proof']['fact_rows'] = '13'
            _write_json(verify_path, verify)
            native['verification_report']['sha256'] = preflight.sha(verify_path)
            _write_json(fixture['receipt'], receipt_doc)
            with self.assertRaisesRegex(ValueError, 'does not prove the bound native version/counts'):
                preflight.validate_archive_receipt(fixture['receipt'])

    def test_fixed_shared_rust_target_is_detected_without_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend = root / 'backend'
            target = root / 'Library' / 'Caches' / 'TraceFang' / 'rust-target' / 'release'
            target.mkdir(parents=True)
            executable = target / 'legacy_probe'
            executable.write_bytes(b'fixed artifact')
            executable.chmod(0o500)
            with mock.patch.object(sealer.Path, 'home', return_value=root), \
                    mock.patch.dict(sealer.os.environ, {}, clear=True):
                self.assertTrue(sealer.in_mutable_target(executable.resolve(), backend.resolve()))
                with self.assertRaisesRegex(ValueError, 'requires its fixed build receipt SHA'):
                    sealer.validate_tool_sources({'probe': executable.resolve()}, {}, backend.resolve())

    def test_cargo_target_executor_requires_and_checks_fixed_receipt_sha(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend = root / 'backend'
            target = backend / 'target' / 'release'
            target.mkdir(parents=True)
            executable = target / 'legacy_probe'
            executable.write_bytes(b'fixed cargo artifact')
            executable.chmod(0o500)
            digest = sealer.sha(executable)
            sources = {'probe': executable.resolve()}
            with self.assertRaisesRegex(ValueError, 'requires its fixed build receipt SHA'):
                sealer.validate_tool_sources(sources, {}, backend.resolve())
            self.assertEqual(sealer.validate_tool_sources(
                sources, {'probe': digest}, backend.resolve())['probe']['authority_sha256'], digest)
            with self.assertRaisesRegex(ValueError, 'fixed authority SHA'):
                sealer.validate_tool_sources(sources, {'probe': 'f' * 64}, backend.resolve())

    def test_spool_audit_requires_full_fixed_prefix_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spool = root / 'spool.bin'
            spool.write_bytes(b'ordered-spool')
            digest = terminal.sha(spool)
            fixed = 'f' * 64
            build = 'b' * 64
            tail = {'epoch': 'stream:1', 'sequence': 2}
            proof = {'manifest': {
                'schema': 'retained-global-decoded-spool-v2', 'complete': True,
                'source_manifest_sha256': fixed, 'build_sha256': build,
                'first': {'epoch': 'stream:1', 'sequence': 1}, 'last': tail, 'frames': '2',
                'original_prefix_complete': False, 'origin_coverage': {'original_prefix_complete': False},
                'canonical_decoded_sha256': 'c' * 64},
                'original_complete_decoded_roundtrip': {'complete': True, 'frames': '2',
                    'expected_sha256': 'c' * 64, 'actual_sha256': 'c' * 64},
                'spool_file_sha256': digest, 'created': False,
                'production_modified': False, 'authority_boundary_created': False}
            report = root / 'audit.json'
            report.write_text(json.dumps(proof))
            checked = terminal.validate_spool_audit(report, fixed, build,
                {'raw': {'native_mapping': {'last_position': tail}}}, spool)
            self.assertEqual(checked['manifest']['frames'], '2')
            proof['original_complete_decoded_roundtrip']['actual_sha256'] = 'd' * 64
            report.write_text(json.dumps(proof))
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                terminal.validate_spool_audit(report, fixed, build,
                    {'raw': {'native_mapping': {'last_position': tail}}}, spool)

    def test_stop_validator_rejects_handwritten_or_incomplete_report(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'stop.json'
            path.write_text(json.dumps({'schema': 'legacy-stop-evidence-v1',
                'production_terminal': True, 'raw_producers_stopped': True,
                'stopped_component_ids': ['service']}))
            with self.assertRaises((KeyError, ValueError, FileNotFoundError)):
                terminal.stop_input(path)


if __name__ == '__main__':
    unittest.main()
