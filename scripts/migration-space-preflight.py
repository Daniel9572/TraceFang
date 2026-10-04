#!/usr/bin/env python3
"""Read-only, phase-local disk gate. This script never stops or changes providers."""
import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from datetime import datetime, timezone

GIB = 1024**3
NATIVE_SPOOL_CAP_GIB = 5
SHA = re.compile(r'[0-9a-f]{64}\Z')
RECEIPT_SCHEMA = 'tracefang-archived-input-adapter-v1'
RESTORE_SCHEMA = 'tracefang-restore-manifest-v1'
GROUPS = {
    'facts': {'old-7051-facts', 'corrected-rehearsal-facts'},
    'postgres_table': {'retired-original-pg-table-pairs'},
    'canonical': {'retired-original-canonical'},
}
GROUP_KIND = {group_id: kind for kind, group_ids in GROUPS.items() for group_id in group_ids}
MAX_ARCHIVE_STREAM_BYTES = 64 * 1024**3


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _without_atime(value):
    if isinstance(value, dict):
        return {key: _without_atime(item) for key, item in value.items()
                if key not in ('atime', 'atime_ns')}
    if isinstance(value, list):
        return [_without_atime(item) for item in value]
    return value


def _validate_xattrs(metadata, group_id):
    xattrs = metadata.get('xattrs', {})
    if not isinstance(xattrs, dict):
        raise ValueError(f'{group_id} source xattr inventory is malformed')
    for name, record in xattrs.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise ValueError(f'{group_id} source xattr record is malformed')
        encoded = record.get('base64')
        if not isinstance(encoded, str):
            raise ValueError(f'{group_id} source xattr lacks its base64 value')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError(f'{group_id} source xattr base64 is invalid') from exc
        if (_decimal(record.get('length'), f'{group_id} xattr length') != len(raw)
                or require_sha(record.get('sha256'), f'{group_id} xattr SHA')
                != hashlib.sha256(raw).hexdigest()):
            raise ValueError(f'{group_id} source xattr bytes do not match length/SHA')


def stable_read(path):
    path = Path(path)
    before = os.stat(path, follow_symlinks=False)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f'input must be a regular non-symlink file: {path}')
    chunks = []
    count = 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if _identity(opened) != _identity(before):
            raise ValueError(f'input identity changed before read: {path}')
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            chunks.append(block)
            count += len(block)
            if count > 64 * 1024 * 1024:
                raise ValueError(f'JSON evidence exceeds the 64MiB read bound: {path}')
        after_fd = os.fstat(stream.fileno())
    after = os.stat(path, follow_symlinks=False)
    if count != before.st_size or _identity(after_fd) != _identity(before) or _identity(after) != _identity(before):
        raise ValueError(f'input was short-read or changed during read: {path}')
    return b''.join(chunks)


def sha(path):
    path = Path(path)
    before = os.stat(path, follow_symlinks=False)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f'input must be a regular non-symlink file: {path}')
    digest = hashlib.sha256()
    count = 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if _identity(opened) != _identity(before):
            raise ValueError(f'input identity changed before hash: {path}')
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            count += len(block)
            digest.update(block)
        after_fd = os.fstat(stream.fileno())
    after = os.stat(path, follow_symlinks=False)
    if count != before.st_size or _identity(after_fd) != _identity(before) or _identity(after) != _identity(before):
        raise ValueError(f'input was short-hashed or changed during read: {path}')
    return digest.hexdigest()


def _file_ref(directory, record, label):
    if not isinstance(record, dict) or not isinstance(record.get('file'), str):
        raise ValueError(f'{label} artifact reference is missing a file')
    require_sha(record.get('sha256'), f'{label} SHA')
    candidate = Path(record['file'])
    if not candidate.is_absolute():
        candidate = Path(directory) / candidate
    if candidate.is_symlink():
        raise ValueError(f'{label} must be a regular non-symlink file')
    path = candidate.resolve(strict=True)
    if not path.is_file() or sha(path) != record['sha256']:
        raise ValueError(f'{label} bytes do not match their SHA')
    return path


def require_sha(value, label):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError(f'{label} must be a lowercase SHA-256')
    return value


def _decimal(value, label):
    if not isinstance(value, (str, int)) or not str(value).isdigit():
        raise ValueError(f'{label} must be a nonnegative decimal integer')
    return int(value)


def _original_path(value):
    if isinstance(value, Path):
        value = str(value)
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('restore manifest original paths must be absolute')
    path = Path(os.path.normpath(value))
    if str(path) != value:
        raise ValueError('restore manifest original paths must be normalized')
    if path.is_symlink():
        raise ValueError('original archive paths cannot be symlinks')
    return str(path)


def _path_key(value):
    return str(Path(_original_path(value)).resolve())


def _verify_gzip_stream(path, expected_sha, expected_bytes):
    """Re-read every gzip member and verify its full decompressed source bytes."""
    path = Path(path)
    compressed_sha = sha(path)
    before = os.stat(path, follow_symlinks=False)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'archive must be a regular non-symlink file: {path}')
    digest = hashlib.sha256()
    count = 0
    with path.open('rb') as raw:
        if _identity(os.fstat(raw.fileno())) != _identity(before):
            raise ValueError(f'archive changed before independent gzip read: {path}')
        with gzip.GzipFile(fileobj=raw, mode='rb') as source:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                count += len(block)
                if count > MAX_ARCHIVE_STREAM_BYTES:
                    raise ValueError(f'archive stream exceeds the 64GiB limit: {path}')
                digest.update(block)
        after_fd = os.fstat(raw.fileno())
    after = os.stat(path, follow_symlinks=False)
    final_sha = sha(path)
    actual_sha = digest.hexdigest()
    if (_identity(after_fd) != _identity(before) or _identity(after) != _identity(before)
            or final_sha != compressed_sha or actual_sha != expected_sha or count != expected_bytes):
        raise ValueError(f'archive full-stream SHA/length or stable identity verification failed: {path}')
    return {'sha256': actual_sha, 'bytes': count}


def _artifact_sha(record, label):
    if not isinstance(record, dict) or not isinstance(record.get('file'), str):
        raise ValueError(f'{label} artifact reference is required')
    return require_sha(record.get('sha256'), f'{label} SHA')


def _proof_candidates(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _proof_candidates(item)
    elif isinstance(value, list):
        for item in value:
            yield from _proof_candidates(item)


def _normalize_alias_restore(restore, restore_path, group_id, artifact_rebinds=()):
    """Read the real multi-alias producer shape without changing its receipt."""
    if group_id not in ('retired-original-pg-table-pairs', 'retired-original-canonical'):
        raise ValueError('multi-alias producer shape is only eligible for original source bodies')
    entries = restore.get('entries')
    if (not isinstance(entries, list) or not entries
            or restore.get('all_known_nlinks_accounted_for') is not True
            or restore.get('all_source_aliases_retained') is not True
            or Path(restore.get('restore_manifest_path', '')).resolve() != restore_path):
        raise ValueError(f'{group_id} alias producer inventory is incomplete')
    refs = restore.get('source_manifest_and_descriptor_sha256')
    refs_after = restore.get('source_manifest_and_descriptor_metadata_after')
    if not isinstance(refs, list) or not isinstance(refs_after, list):
        raise ValueError(f'{group_id} source manifest metadata inventory is missing')
    by_role = {}
    overrides = {item.get('role'): item for item in artifact_rebinds}
    if (len(overrides) != len(artifact_rebinds)
            or set(overrides) - {'corrected_sources_progress_manifest'}):
        raise ValueError('only mutable corrected progress metadata may use a source artifact rebind')
    metadata_keys = {'path', 'identity', 'allocated_bytes', 'mode', 'full_mode_octal',
                     'uid', 'gid', 'mtime_ns', 'ctime_ns', 'birthtime_ns',
                     'flags', 'nlink', 'xattrs', 'acl_flags_listing'}
    for ref in refs:
        role = ref.get('role')
        if role in by_role or role not in {
                'original_source_manifest', 'corrected_sources_progress_manifest',
                'original_canonical_descriptor'}:
            raise ValueError(f'{group_id} source manifest roles are invalid')
        override = overrides.get(role)
        logical_path = Path(_path_key(ref.get('path')))
        if override is not None:
            if (override.get('historical_path') != ref.get('path')
                    or override.get('historical_sha256') != ref.get('sha256')):
                raise ValueError('source artifact rebind belongs to another historical progress manifest')
            path = _file_ref(restore_path.parent, override.get('verified_current_copy'),
                             f'{group_id} current progress metadata copy')
        else:
            path = _file_ref(restore_path.parent, {'file': ref.get('path'),
                            'sha256': ref.get('sha256')}, f'{group_id} {role}')
        before_ref, after_ref = ref.get('metadata_before'), ref.get('metadata_after_read')
        if (not isinstance(before_ref, dict) or not isinstance(after_ref, dict)
                or not metadata_keys.issubset(before_ref)
                or not metadata_keys.issubset(after_ref)
                or _path_key(before_ref['path']) != str(logical_path)
                or _path_key(after_ref['path']) != str(logical_path)
                or before_ref['identity'].get('size') != _decimal(ref.get('bytes'), 'historical manifest length')):
            raise ValueError(f'{group_id} source manifest metadata is incomplete')
        _validate_xattrs(before_ref, group_id)
        _validate_xattrs(after_ref, group_id)
        if ((override is None and _decimal(ref.get('bytes'), 'source manifest length') != path.stat().st_size)
                or _without_atime(ref.get('metadata_before'))
                != _without_atime(ref.get('metadata_after_read'))):
            raise ValueError(f'{group_id} source manifest metadata changed')
        by_role[role] = (logical_path, json.loads(stable_read(path)), ref)
    after_refs = {ref.get('role'): ref for ref in refs_after}
    if (len(after_refs) != len(refs_after) or set(after_refs) != set(by_role)
            or any(_without_atime({key: after_refs[role].get(key) for key in
                                   ('path', 'role', 'sha256', 'bytes', 'metadata_before', 'metadata_after_read')})
                   != _without_atime({key: item[2].get(key) for key in
                                      ('path', 'role', 'sha256', 'bytes', 'metadata_before', 'metadata_after_read')})
                   for role, item in by_role.items())
            or set(by_role) != {'original_source_manifest',
                               'corrected_sources_progress_manifest',
                               'original_canonical_descriptor'}):
        raise ValueError(f'{group_id} final source manifest metadata inventory changed')
    pg_paths = {}
    original_manifest = by_role['original_source_manifest'][1]
    for role in ('original_source_manifest', 'corrected_sources_progress_manifest'):
        manifest_path, manifest, _ = by_role[role]
        if (manifest.get('id') != original_manifest.get('id')
                or manifest.get('postgres', {}).get('snapshot')
                != original_manifest.get('postgres', {}).get('snapshot')):
            raise ValueError(f'{group_id} source aliases belong to different PostgreSQL snapshots')
        for table in manifest.get('tables', []):
            key = str((manifest_path.parent / table['file']).resolve())
            digest = require_sha(table.get('sha256'), 'source table SHA')
            if key in pg_paths and pg_paths[key] != digest:
                raise ValueError(f'{group_id} source manifests disagree on a table')
            pg_paths[key] = digest
    final_metadata = restore.get('source_metadata_after_all_files')
    if not isinstance(final_metadata, list):
        raise ValueError(f'{group_id} final alias metadata inventory is missing')
    final_by_path = {_path_key(item.get('path')): item for item in final_metadata}
    if len(final_by_path) != len(final_metadata):
        raise ValueError(f'{group_id} final alias metadata repeats a path')
    files, all_aliases, inodes = [], set(), set()
    for entry in entries:
        primary = _path_key(entry.get('primary_alias'))
        aliases = entry.get('alias_paths')
        if not isinstance(aliases, list) or not aliases:
            raise ValueError(f'{group_id} producer omits aliases')
        alias_set = {_path_key(alias) for alias in aliases}
        nlink = _decimal(entry.get('expected_nlink'), 'source hardlink count')
        if (primary not in alias_set or len(alias_set) != len(aliases)
                or len(aliases) != nlink
                or _decimal(entry.get('alias_count'), 'source alias count') != nlink
                or all_aliases.intersection(alias_set)):
            raise ValueError(f'{group_id} producer alias inventory does not account for every hardlink')
        before = entry.get('source_metadata_before_by_alias')
        after = entry.get('source_metadata_after_by_alias')
        if not isinstance(before, list) or not isinstance(after, list):
            raise ValueError(f'{group_id} producer alias metadata is missing')
        before_map = {_path_key(item.get('path')): item for item in before}
        after_map = {_path_key(item.get('path')): item for item in after}
        if (len(before_map) != len(before) or len(after_map) != len(after)
                or set(before_map) != alias_set or set(after_map) != alias_set):
            raise ValueError(f'{group_id} producer alias metadata omits a hardlink')
        identity = entry.get('source_inode_identity') or {}
        inode = (identity.get('device'), identity.get('inode'))
        if None in inode or inode in inodes:
            raise ValueError(f'{group_id} producer repeats an archived inode')
        inodes.add(inode)
        source_bytes = _decimal(entry.get('logical_bytes'), 'unique source length')
        source_sha = require_sha(entry.get('source_sha256'), 'unique source SHA')
        if (identity.get('size') != source_bytes
                or entry.get('expected_recorded_sha256') != source_sha):
            raise ValueError(f'{group_id} producer source identity/SHA is inconsistent')
        for alias in alias_set:
            left, right = before_map[alias], after_map[alias]
            if (not metadata_keys.issubset(left) or not metadata_keys.issubset(right)
                    or left.get('identity') != identity or right.get('identity') != identity
                    or left.get('nlink') != nlink or right.get('nlink') != nlink
                    or _without_atime(left) != _without_atime(right)
                    or _without_atime(final_by_path.get(alias)) != _without_atime(right)):
                raise ValueError(f'{group_id} alias metadata identity changed')
            _validate_xattrs(left, group_id)
            _validate_xattrs(right, group_id)
            if GROUP_KIND[group_id] == 'postgres_table' and pg_paths.get(alias) != source_sha:
                raise ValueError(f'{group_id} alias body is absent from its bound source manifest')
        all_aliases.update(alias_set)
        # These are in-memory field translations. Completed stream and handle
        # claims remain the original producer values and are checked below.
        files.append({**entry, 'source_path': primary, 'source_bytes': source_bytes,
                      'source_metadata_before': before_map[primary],
                      'source_metadata_after': after_map[primary],
                      'compressed_size': entry.get('archive_size'), 'status': 'verified'})
    if (set(final_by_path) != all_aliases
            or _decimal(restore.get('unique_source_inode_count'), 'unique inode count') != len(files)
            or _decimal(restore.get('source_alias_paths_count'), 'total alias count') != len(all_aliases)):
        raise ValueError(f'{group_id} producer final alias/inode totals do not reconcile')
    return {**restore, 'files': files,
            'source_directory': str(Path(files[0]['source_path']).parent),
            'compressed_bytes_total': restore.get('archive_compressed_bytes_total'),
            'logical_source_bytes': restore.get('logical_source_bytes_unique'),
            'source_metadata_preserved_except_possible_atime': True,
            'all_source_output_and_gzip_handles_closed': all(
                entry.get(key) is True for entry in entries
                for key in ('source_fd_closed', 'archive_fd_closed', 'gzip_handle_closed')),
            'output_directory_fsync_succeeded': all(
                entry.get('archive_directory_fsync_succeeded') is True for entry in entries)}


def _validate_source_binding(receipt_directory, group_id, group, restore_entries):
    binding = group.get('source_binding') or {}
    source_ref = binding.get('source_manifest')
    source_path = _file_ref(receipt_directory, source_ref, f'{group_id} source manifest')
    source = json.loads(stable_read(source_path))
    if (not isinstance(source.get('id'), str) or not source.get('id')
            or binding.get('source_manifest_id') != source.get('id')
            or not isinstance(source.get('postgres'), dict)
            or binding.get('postgres_snapshot') != source['postgres'].get('snapshot')):
        raise ValueError(f'{group_id} adapter does not bind the actual PostgreSQL source manifest/snapshot')
    binding_result = {'source_manifest': {'file': str(source_path), 'sha256': sha(source_path)},
                      'source_manifest_id': source['id'],
                      'postgres_snapshot': source['postgres']['snapshot']}
    kind = GROUP_KIND[group_id]
    if kind == 'postgres_table':
        table_paths = {str((source_path.parent / item['file']).resolve()): item['sha256']
                       for item in source.get('tables', [])
                       if isinstance(item, dict) and isinstance(item.get('file'), str)}
        for entry in restore_entries:
            if table_paths.get(str(Path(entry['source_path']).resolve())) != entry['source_sha256']:
                raise ValueError('archived PostgreSQL body is absent from the bound source manifest')
    elif kind == 'canonical':
        descriptor_path = _file_ref(receipt_directory, binding.get('canonical_descriptor'),
                                    f'{group_id} canonical descriptor')
        descriptor = json.loads(stable_read(descriptor_path))
        if (descriptor_path.parent != source_path.parent
                or descriptor.get('source_manifest_id') != source.get('id')
                or descriptor.get('snapshot') != source['postgres']['snapshot']
                or binding['canonical_descriptor'].get('sha256') != sha(descriptor_path)):
            raise ValueError('canonical archive adapter does not bind the actual source descriptor')
        canonical_paths = {str((descriptor_path.parent / item['file']).resolve()): item['sha256']
                           for item in descriptor.get('source_tables', {}).values()
                           if isinstance(item, dict) and isinstance(item.get('file'), str)}
        canonical_file = descriptor.get('file')
        if isinstance(canonical_file, str):
            canonical_paths[str((descriptor_path.parent / canonical_file).resolve())] = descriptor.get('sha256')
        for entry in restore_entries:
            if canonical_paths.get(str(Path(entry['source_path']).resolve())) != entry['source_sha256']:
                raise ValueError('archived canonical body is absent from the bound source descriptor')
        binding_result['canonical_descriptor'] = {
            'file': str(descriptor_path), 'sha256': sha(descriptor_path)}
    else:
        native = binding.get('native_facts') or {}
        native_path = _path_key(native.get('source_path'))
        if (native_path not in {_path_key(entry['source_path']) for entry in restore_entries}
                or not all(isinstance(native.get(key), str) and native[key]
                           for key in ('generation', 'commit_id', 'store_epoch',
                                       'schema_version', 'aggregation_version'))
                or native.get('generation') != 'legacy-' + source['id']):
            raise ValueError(f'{group_id} archive lacks its exact native facts source/version')
        native_fact_rows = _decimal(native.get('fact_rows'), 'native fact row count')
        native_index_nodes = _decimal(native.get('index_nodes'), 'native index node count')
        fact_sha = require_sha(native.get('fact_codec_sha256'), 'native fact codec SHA')
        index_sha = require_sha(native.get('index_codec_sha256'), 'native index codec SHA')
        verification_ref = native.get('verification_report')
        verification_path = _file_ref(receipt_directory, verification_ref,
                                      f'{group_id} native verification report')
        verification = json.loads(stable_read(verification_path))
        matches = []
        for candidate in _proof_candidates(verification):
            commit = candidate.get('verified_commit_id', candidate.get('commit_id'))
            if (candidate.get('complete') is True
                    and candidate.get('index_verified') is True
                    and candidate.get('generation') == native['generation']
                    and str(commit) == native['commit_id']
                    and candidate.get('store_epoch') == native['store_epoch']
                    and candidate.get('schema_version') == native['schema_version']
                    and candidate.get('aggregation_version') == native['aggregation_version']
                    and _decimal(candidate.get('fact_rows'), 'verified fact row count') == native_fact_rows
                    and _decimal(candidate.get('index_nodes'), 'verified index node count') == native_index_nodes
                    and candidate.get('fact_codec_sha256') == fact_sha
                    and candidate.get('index_codec_sha256') == index_sha):
                matches.append(candidate)
        if not matches:
            raise ValueError(f'{group_id} native verification report does not prove the bound native version/counts')
        reopen_ref = native.get('reopen_report')
        reopen_path = _file_ref(receipt_directory, reopen_ref,
                                f'{group_id} independent reopen report')
        reopen = json.loads(stable_read(reopen_path))
        active_version = reopen.get('active_version') or {}
        reopen_matches = []
        if active_version.get('store_epoch') == native['store_epoch']:
            for candidate in _proof_candidates(reopen):
                if (candidate.get('complete') is True
                        and candidate.get('index_verified') is True
                        and candidate.get('schema_version') == native['schema_version']
                        and candidate.get('aggregation_version') == native['aggregation_version']
                        and _decimal(candidate.get('fact_rows'), 'reopened fact row count') == native_fact_rows
                        and _decimal(candidate.get('index_nodes'), 'reopened index node count') == native_index_nodes
                        and candidate.get('fact_codec_sha256') == fact_sha
                        and candidate.get('index_codec_sha256') == index_sha):
                    reopen_matches.append(candidate)
        if not reopen_matches:
            raise ValueError(f'{group_id} independent reopen report does not match the native version/counts')
        binding_result['native_facts'] = {
            'source_path': native_path, 'generation': native['generation'],
            'commit_id': native['commit_id'], 'store_epoch': native['store_epoch'],
            'schema_version': native['schema_version'],
            'aggregation_version': native['aggregation_version'],
            'fact_rows': str(native_fact_rows), 'index_nodes': str(native_index_nodes),
            'fact_codec_sha256': fact_sha,
            'index_codec_sha256': index_sha,
            'verification_report': {'file': str(verification_path), 'sha256': sha(verification_path)},
            'reopen_report': {'file': str(reopen_path), 'sha256': sha(reopen_path)},
        }
    return binding_result


def verify_source_manifest_binding(receipt, manifest_path, manifest, canonical_descriptor_path=None,
                                   required_group_ids=()):
    """Bind only archived PG/canonical bodies that replaced this snapshot's files."""
    required_group_ids = set(required_group_ids)
    if receipt is None or not required_group_ids:
        return
    manifest_path = Path(manifest_path).resolve(strict=True)
    manifest_sha = sha(manifest_path)
    matched = set()
    for group in receipt.get('groups', []):
        if group.get('group_id') not in required_group_ids:
            continue
        binding = group.get('source_binding') or {}
        source_ref = binding.get('source_manifest') or {}
        if (source_ref.get('file') != str(manifest_path)
                or source_ref.get('sha256') != manifest_sha
                or binding.get('source_manifest_id') != manifest.get('id')
                or binding.get('postgres_snapshot') != (manifest.get('postgres') or {}).get('snapshot')):
            raise ValueError(f"{group['group_id']} archive adapter belongs to another source manifest/snapshot")
        matched.add(group['group_id'])
        if group['group_id'] == 'retired-original-canonical':
            descriptor_ref = binding.get('canonical_descriptor') or {}
            if canonical_descriptor_path is None:
                raise ValueError('archived canonical requires its still-readable source descriptor')
            descriptor_path = Path(canonical_descriptor_path).resolve(strict=True)
            if (descriptor_ref.get('file') != str(descriptor_path)
                    or descriptor_ref.get('sha256') != sha(descriptor_path)):
                raise ValueError('archived canonical adapter belongs to another source descriptor')
    if matched != required_group_ids:
        raise ValueError('archived PG/canonical input lacks its source-manifest binding')


def _current_rebind(directory, group_ref, restore_path, restore_entries):
    reference = group_ref.get('current_identity_rebind')
    if reference is None:
        return {}, None
    path = _file_ref(directory, reference, 'current archive identity rebind')
    proof = json.loads(stable_read(path))
    if (proof.get('schema') != 'tracefang-archive-current-identity-rebind-v1'
            or proof.get('complete') is not True
            or proof.get('all_read_handles_closed') is not True
            or proof.get('current_source_content_matches_producer_and_independent_decode') is not True
            or proof.get('all_current_source_alias_metadata_stable') is not True
            or proof.get('producer_restore_manifests_modified') is not False
            or _decimal(proof.get('archive_credit_bytes'), 'rebind archive credit') != 0):
        raise ValueError('current archive identity rebind lacks complete source-content proof')
    matches = [group for group in proof.get('groups', [])
               if group.get('group_id') == group_ref['group_id']]
    if (len(matches) != 1 or matches[0].get('restore_manifest') != group_ref['restore_manifest']
            or Path(matches[0]['restore_manifest']['file']).resolve() != restore_path):
        raise ValueError('current identity rebind belongs to another native restore manifest')
    records = matches[0].get('entries')
    if not isinstance(records, list) or len(records) != len(restore_entries):
        raise ValueError('current identity rebind must account for every archived inode')
    by_path = {_path_key(item.get('source_path')): item for item in records}
    if len(by_path) != len(records):
        raise ValueError('current identity rebind repeats a source inode')
    adapter_aliases = {_path_key(entry['source_path']): {_path_key(alias) for alias in entry['aliases']}
                      for entry in group_ref['entries']}
    for native in restore_entries:
        source = _path_key(native['source_path'])
        item = by_path.get(source) or {}
        source_proof = item.get('source_device_rebind_proof')
        if source_proof is not None:
            root_path = _file_ref(directory, source_proof, 'actual oldfacts full-source identity proof')
            root_proof = json.loads(stable_read(root_path))
            if (root_proof.get('schema') != 'tracefang-old7051-source-device-rebind-v1'
                    or root_proof.get('status') != 'verified'
                    or root_proof.get('identity_stable') is not True
                    or _path_key(root_proof.get('source_path')) != source
                    or root_proof.get('source_sha256') != native['source_sha256']
                    or root_proof.get('source_bytes_read_once') != native['source_bytes']
                    or root_proof.get('source_identity_before') != root_proof.get('source_identity_after')
                    or root_proof.get('opened_identity_before') != root_proof.get('opened_identity_after')
                    or root_proof.get('native_facts') != group_ref['source_binding']['native_facts']
                    or root_proof.get('immutable_manifest', {}).get('sha256') != group_ref['restore_manifest']['sha256']):
                raise ValueError('oldfacts current identity proof does not bind the full native source')
        aliases = {_path_key(alias) for alias in item.get('aliases', [])}
        if (aliases != adapter_aliases.get(source) or len(aliases) != len(item.get('aliases', []))
                or item.get('source_sha256') != native['source_sha256']
                or _decimal(item.get('source_bytes'), 'rebound source length') != native['source_bytes']
                or _decimal(item.get('full_source_bytes_rehashed'), 'rebound full read') != native['source_bytes']
                or item.get('full_source_sha256_read_complete') is not True
                or item.get('current_aliases_same_inode_and_device') is not True
                or item.get('all_source_handles_closed') is not True):
            raise ValueError('current identity rebind source bytes/aliases are not fully proved')
        before = item.get('source_metadata_before_by_alias')
        after = item.get('source_metadata_after_by_alias')
        if not isinstance(before, list) or not isinstance(after, list):
            raise ValueError('current identity rebind alias metadata is missing')
        left = {_path_key(value.get('path')): value for value in before}
        right = {_path_key(value.get('path')): value for value in after}
        if (set(left) != aliases or set(right) != aliases
                or len(left) != len(before) or len(right) != len(after)
                or _without_atime(left) != _without_atime(right)):
            raise ValueError('current identity rebind alias metadata changed or omits a path')
        identities = [value.get('identity') for value in before]
        if (not identities or not isinstance(identities[0], dict)
                or identities[0].get('size') != native['source_bytes']
                or any(value != identities[0] for value in identities)
                or any(value.get('nlink') != len(aliases) for value in before)):
            raise ValueError('current identity rebind aliases no longer share one inode')
        for value in before + after:
            _validate_xattrs(value, group_ref['group_id'])
        archive = item.get('archive') or {}
        if (archive.get('sha256') != native['archive_sha256']
                or _path_key(archive.get('file')) != _path_key(native['archive_file'])
                or _decimal(archive.get('bytes'), 'rebound compressed length') != native['compressed_size']
                or archive.get('full_compressed_sha256_read_complete') is not True
                or archive.get('archive_handle_closed') is not True
                or not isinstance(archive.get('metadata_before'), dict)
                or archive.get('metadata_before') != archive.get('metadata_after')):
            raise ValueError('current identity rebind compressed bytes are not fully proved')
    if set(by_path) != {_path_key(item['source_path']) for item in restore_entries}:
        raise ValueError('current identity rebind contains an unknown source path')
    return by_path, {'file': str(path), 'sha256': sha(path)}


def validate_archive_receipt(receipt_path, verify_streams=False):
    """Validate an adapter bound to native per-file gzip restore manifests.

    The native manifest remains the only source of compression, metadata, and
    full-stream claims. The adapter adds only source-domain and alias links.
    """
    receipt_path = Path(receipt_path)
    if receipt_path.is_symlink():
        raise ValueError('archived-input receipt cannot be a symlink')
    receipt_path = receipt_path.resolve(strict=True)
    if not receipt_path.is_file():
        raise ValueError('archived-input receipt must be a regular file')
    receipt = json.loads(stable_read(receipt_path))
    groups = receipt.get('groups')
    if not (receipt.get('schema') == RECEIPT_SCHEMA and receipt.get('complete') is True
            and _decimal(receipt.get('archive_credit_bytes'), 'adapter archive credit') == 0
            and receipt.get('originals_deleted_or_moved') is False
            and receipt.get('source_of_stream_and_metadata_claims') ==
                'immutable producer restore_manifest only; adapter does not add or override these claims'
            and isinstance(groups, list) and groups):
        raise ValueError('a complete named lossless archive receipt is required')
    by_path = {}
    verified_groups = []
    ids = set()
    for group_ref in groups:
        group_id = group_ref.get('group_id')
        if not isinstance(group_id, str) or group_id in ids:
            raise ValueError('archive adapter group IDs must be unique names')
        if group_id not in GROUP_KIND:
            raise ValueError(f'archive group {group_id!r} is not eligible as a base input')
        ids.add(group_id)
        restore_path = _file_ref(receipt_path.parent, group_ref.get('restore_manifest'),
                                 f'{group_id} native restore manifest')
        restore = json.loads(stable_read(restore_path))
        alias_shape = isinstance(restore.get('entries'), list)
        if alias_shape:
            native_refs = {item['role']: item
                           for item in restore.get('source_manifest_and_descriptor_sha256', [])}
            for role, key in [('original_source_manifest', 'source_manifest'),
                              ('original_canonical_descriptor', 'canonical_descriptor')]:
                if key == 'canonical_descriptor' and GROUP_KIND[group_id] != 'canonical':
                    continue
                native_ref = native_refs.get(role) or {}
                binding_ref = (group_ref.get('source_binding') or {}).get(key) or {}
                if (binding_ref.get('sha256') != native_ref.get('sha256')
                        or _path_key(binding_ref.get('file')) != _path_key(native_ref.get('path'))):
                    raise ValueError(f'{group_id} adapter source binding differs from producer')
            overrides = group_ref.get('source_artifact_rebinds', [])
            if overrides and not group_ref.get('current_identity_rebind'):
                raise ValueError('progress artifact rebind requires actual complete source-content rebind')
            restore = _normalize_alias_restore(restore, restore_path, group_id, overrides)
        release = restore.get('release') or {}
        restore_entries = restore.get('files')
        multi_file = isinstance(restore_entries, list)
        if not multi_file and isinstance(restore.get('source_path'), str):
            # old-7051 producer receipts use the same native schema with one
            # archive recorded directly on the manifest root.
            restore_entries = [restore]
        all_handles_closed = (restore.get('all_source_output_and_gzip_handles_closed') is True
            if multi_file else all(restore.get(key) is True for key in
                ('source_fd_closed', 'archive_fd_closed', 'gzip_handle_closed')))
        output_directory_synced = (restore.get('output_directory_fsync_succeeded') is True
            if multi_file else restore.get('archive_directory_fsync_succeeded') is True)
        if (not multi_file and
                (restore.get('restore_manifest_path') is None
                 or Path(restore['restore_manifest_path']).resolve(strict=True) != restore_path)):
            raise ValueError(f'{group_id} native restore-manifest path is inconsistent')
        if (restore.get('schema') != RESTORE_SCHEMA or restore.get('status') != 'complete'
                or restore.get('group_id') != group_id or not restore.get('durable_location')
                or restore.get('source_identity_preserved') is not True
                or restore.get('source_metadata_preserved_except_possible_atime') is not True
                or not all_handles_closed or not output_directory_synced
                or restore.get('restore_manifest_fsync_succeeded') is not True
                or _decimal(restore.get('archive_credit_bytes'), 'native archive credit') != 0
                or _decimal(release.get('archive_credit_bytes'), 'native release archive credit') != 0
                or release.get('originals_deleted_or_moved') is not False
                or _decimal(release.get('space_released_bytes'), 'native released bytes') != 0
                or (not multi_file and
                    _decimal(restore.get('space_released_bytes'), 'native released bytes') != 0)
                or not isinstance(restore_entries, list) or not restore_entries):
            raise ValueError(f'{group_id} native restore manifest is incomplete or reports archive credit')
        adapter_entries = group_ref.get('entries')
        if not isinstance(adapter_entries, list) or len(adapter_entries) != len(restore_entries):
            raise ValueError(f'{group_id} adapter must map every native archived inode exactly once')
        rebound, rebind_ref = _current_rebind(receipt_path.parent, group_ref, restore_path, restore_entries)
        source_directory = _original_path(restore.get('source_directory') or
                                          Path(restore_entries[0]['source_path']).parent)
        archive_directory = _original_path(restore.get('archive_directory'))
        adapter_by_source = {}
        for adapter_entry in adapter_entries:
            source_name = _path_key(adapter_entry.get('source_path'))
            if source_name in adapter_by_source:
                raise ValueError(f'{group_id} adapter repeats an archived source path')
            adapter_by_source[source_name] = adapter_entry
        file_records = []
        found_aliases = set()
        total_compressed = total_source = total_allocated = 0
        for native_entry in restore_entries:
            original_source_name = _original_path(native_entry.get('source_path'))
            source_name = _path_key(original_source_name)
            if Path(source_name).parent != Path(source_directory).resolve():
                raise ValueError(f'{group_id} native source path escapes its recorded source directory')
            adapter_entry = adapter_by_source.get(source_name)
            if adapter_entry is None or adapter_entry.get('kind') != GROUP_KIND[group_id]:
                raise ValueError(f'{group_id} adapter entry does not match the native source file')
            aliases = adapter_entry.get('aliases')
            if not isinstance(aliases, list) or not aliases:
                raise ValueError(f'{group_id} adapter omits original hardlink aliases')
            normalized_aliases = {_path_key(alias) for alias in aliases}
            if (alias_shape and normalized_aliases
                    != {_path_key(alias) for alias in native_entry['alias_paths']}):
                raise ValueError(f'{group_id} adapter aliases differ from the native producer inventory')
            metadata_before = native_entry.get('source_metadata_before') or {}
            metadata_after = native_entry.get('source_metadata_after') or {}
            before_identity = metadata_before.get('identity') or {}
            after_identity = metadata_after.get('identity') or {}
            nlink = _decimal(metadata_before.get('nlink'), f'{group_id} source nlink')
            source_bytes = _decimal(native_entry.get('source_bytes'), f'{group_id} source byte count')
            source_sha = require_sha(native_entry.get('source_sha256'), f'{group_id} source SHA')
            if (source_name not in normalized_aliases or len(normalized_aliases) != len(aliases)
                    or len(normalized_aliases) != nlink or found_aliases.intersection(normalized_aliases)):
                raise ValueError(f'{group_id} alias inventory does not match the native hardlink count')
            found_aliases.update(normalized_aliases)
            if (native_entry.get('source_name', Path(source_name).name) != Path(source_name).name
                    or _path_key(metadata_before.get('path')) != source_name
                    or _path_key(metadata_after.get('path')) != source_name
                    or before_identity != after_identity
                    or _without_atime(metadata_before) != _without_atime(metadata_after)
                    or before_identity.get('size') != source_bytes
                    or before_identity.get('mtime_ns') != metadata_before.get('mtime_ns')
                    or metadata_before.get('nlink') != metadata_after.get('nlink')):
                raise ValueError(f'{group_id} original source metadata identity is not stable')
            _validate_xattrs(metadata_before, group_id)
            _validate_xattrs(metadata_after, group_id)
            if not multi_file and (
                    restore.get('atime_before_ns') != metadata_before.get('atime_ns')
                    or restore.get('atime_after_ns') != metadata_after.get('atime_ns')):
                raise ValueError(f'{group_id} native atime fields differ from the full source metadata')
            compressed_sha = require_sha(native_entry.get('archive_sha256'), f'{group_id} compressed SHA')
            compressed_bytes = _decimal(native_entry.get('compressed_size'), f'{group_id} compressed length')
            archive_allocated = _decimal(native_entry.get('archive_allocated_bytes'),
                                         f'{group_id} compressed allocation')
            archive_path = _file_ref(receipt_path.parent,
                {'file': native_entry.get('archive_file'), 'sha256': compressed_sha},
                f'{group_id} compressed archive')
            if Path(archive_path).parent != Path(archive_directory).resolve(strict=True):
                raise ValueError(f'{group_id} compressed archive escapes its recorded archive directory')
            archive_stat = os.stat(archive_path, follow_symlinks=False)
            observed_allocated = getattr(archive_stat, 'st_blocks', 0) * 512 or archive_stat.st_size
            if archive_stat.st_size != compressed_bytes:
                raise ValueError(f'{group_id} compressed archive length differs from native receipt')
            current_archive = (rebound.get(source_name) or {}).get('archive')
            if current_archive is not None:
                current_identity = current_archive['metadata_after']['identity']
                if (current_identity.get('device') != archive_stat.st_dev
                        or current_identity.get('inode') != archive_stat.st_ino
                        or current_identity.get('size') != archive_stat.st_size
                        or current_identity.get('mtime_ns') != archive_stat.st_mtime_ns
                        or current_identity.get('ctime_ns') != archive_stat.st_ctime_ns):
                    raise ValueError(f'{group_id} archive differs from its explicit current identity rebind')
            # Physical allocation may change after fsync on APFS. Keep the
            # producer's historical count and use today's count for capacity;
            # compressed SHA/length and any rebound file identity stay exact.
            output_fd_closed = (native_entry.get('output_fd_closed') is True
                                or native_entry.get('archive_fd_closed') is True)
            required_flags = ('file_fsync_succeeded', 'archive_directory_fsync_succeeded',
                              'gzip_crc_eof_verified', 'gzip_handle_closed', 'source_fd_closed')
            file_status_ok = (native_entry.get('status') == 'verified' if multi_file
                              else restore.get('status') == 'complete')
            if (not file_status_ok
                    or not output_fd_closed
                    or any(native_entry.get(key) is not True for key in required_flags)):
                raise ValueError(f'{group_id} native file entry lacks completed gzip/fsync/close evidence')
            independent_sha = require_sha(native_entry.get('independent_decompressed_sha256'),
                                          f'{group_id} independent stream SHA')
            independent_bytes = _decimal(native_entry.get('independent_decompressed_bytes'),
                                         f'{group_id} independent stream length')
            if independent_sha != source_sha or independent_bytes != source_bytes:
                raise ValueError(f'{group_id} native independent stream proof is incomplete or inconsistent')
            # Normal phase gates hash compressed bytes above and validate the
            # producer's completed independent stream proof. A caller may ask
            # for a second full decompression during a small or scheduled audit.
            if verify_streams:
                streamed = _verify_gzip_stream(archive_path, source_sha, source_bytes)
                if streamed['sha256'] != independent_sha or streamed['bytes'] != independent_bytes:
                    raise ValueError(f'{group_id} actual compressed stream differs from its independent proof')
            for alias in normalized_aliases:
                if alias in by_path:
                    raise ValueError(f'archive adapter repeats original path: {alias}')
                by_path[alias] = {'group_id': group_id, 'kind': GROUP_KIND[group_id],
                    'sha256': source_sha, 'bytes': source_bytes,
                    'allocated_bytes': _decimal(metadata_before.get('allocated_bytes'),
                                                f'{group_id} source allocation'),
                    'archive': {'file': str(archive_path), 'sha256': compressed_sha,
                                'bytes': str(compressed_bytes), 'allocated_bytes': str(observed_allocated),
                                'producer_allocated_bytes': str(archive_allocated)},
                    'restore_manifest': {'file': str(restore_path), 'sha256': sha(restore_path)},
                    'metadata': native_entry}
            total_compressed += compressed_bytes
            total_source += source_bytes
            total_allocated += archive_allocated
            file_records.append({'source_path': source_name, 'source_sha256': source_sha,
                                 'archive_sha256': compressed_sha, 'bytes': str(source_bytes)})
        if set(adapter_by_source) != {item['source_path'] for item in file_records}:
            raise ValueError(f'{group_id} adapter includes paths absent from its native restore manifest')
        recovery = group_ref.get('recovery') or {}
        recovery_scratch = _decimal(recovery.get('scratch_limit_bytes'),
                                    f'{group_id} recovery scratch limit')
        recovery_floor = _decimal(recovery.get('free_floor_bytes'),
                                  f'{group_id} recovery free floor')
        restore_required = _decimal(recovery.get('restore_required_free_bytes'),
                                    f'{group_id} restore-space requirement')
        if (recovery_scratch > 64 * 1024**2 or recovery_floor < 4 * 1024**3
                or restore_required < total_source + recovery_scratch + recovery_floor):
            raise ValueError(f'{group_id} adapter restore-space requirement omits source, scratch or 4GiB floor')
        compressed_total = restore.get('compressed_bytes_total', restore.get('compressed_size'))
        allocated_total = restore.get('archive_allocated_bytes_total', restore.get('archive_allocated_bytes'))
        if (_decimal(compressed_total, 'native compressed total') != total_compressed
                or _decimal(restore.get('logical_source_bytes'), 'native source total') != total_source
                or _decimal(allocated_total, 'native allocated total') != total_allocated
                or _decimal(restore.get('minimum_free_bytes'), 'native recovery floor') < 4 * 1024**3
                or _decimal(restore.get('scratch_disk_bytes'), 'native recovery scratch') > 64 * 1024**2):
            raise ValueError(f'{group_id} native restore totals or recovery-space limits do not reconcile')
        binding_result = _validate_source_binding(receipt_path.parent, group_id, group_ref, file_records)
        verified_groups.append({'group_id': group_id, 'restore_manifest': {
            'file': str(restore_path), 'sha256': sha(restore_path)},
            'archive': [{'file': path['archive']['file'], 'sha256': path['archive']['sha256'],
                         'bytes': path['archive']['bytes'], 'allocated_bytes': path['archive']['allocated_bytes']}
                        for source in file_records for path in [by_path[source['source_path']]]],
            'object_count': str(len(file_records)), 'source_binding': binding_result,
            'current_identity_rebind': rebind_ref,
            'restore_required_free_bytes': str(restore_required),
            'recovery_scratch_limit_bytes': str(recovery_scratch),
            'recovery_free_floor_bytes': str(recovery_floor)})
    return {'path': str(receipt_path), 'sha256': sha(receipt_path),
            'files': by_path, 'groups': verified_groups}


def archived_file(receipt, path, group_kind, expected_sha=None):
    """Return verified original size data for one explicitly allowed absent input."""
    if receipt is None:
        return None
    key = _path_key(path)
    item = receipt['files'].get(key)
    if item is None or item['group_id'] not in GROUPS[group_kind]:
        return None
    if expected_sha is not None and item['sha256'] != expected_sha:
        raise ValueError(f'archived input SHA differs from its source manifest: {path}')
    return item


def input_file(path, label, receipt=None, group_kind=None, expected_sha=None):
    path = Path(os.path.abspath(path))
    if path.is_file() and not path.is_symlink():
        actual = sha(path)
        if expected_sha is not None and actual != expected_sha:
            raise ValueError(f'{label} changed from its source manifest: {path}')
        info = path.stat()
        return {'path': str(path.resolve()), 'kind': 'file', 'bytes': str(info.st_size),
                'allocated_bytes': str(getattr(info, 'st_blocks', 0) * 512 or info.st_size),
                'sha256': actual, 'archived': False}
    if path.exists() or path.is_symlink():
        raise ValueError(f'{label} must be a regular non-symlink file: {path}')
    record = archived_file(receipt, path, group_kind, expected_sha) if group_kind else None
    if record is None:
        raise FileNotFoundError(f'{label} is missing and has no matching verified named archive receipt: {path}')
    return {'path': str(path), 'kind': 'file', 'bytes': str(record['bytes']),
            'allocated_bytes': str(record['allocated_bytes']), 'sha256': record['sha256'],
            'archived': True, 'archive_group_id': record['group_id'],
            'archive': record['archive'], 'restore_manifest': record['restore_manifest'],
            'metadata': record['metadata']}


def allocated(path, missing_ok=False):
    """Report actual allocated bytes without deriving any future savings."""
    if path is None:
        return None
    path = Path(path)
    if path.is_symlink():
        raise ValueError('allocated-input inventory does not follow symlinks')
    try:
        path = path.resolve(strict=True)
    except FileNotFoundError:
        if not missing_ok:
            raise
        return {'path': str(path.resolve()), 'kind': 'not_created',
                'bytes': '0', 'allocated_bytes': '0'}
    if path.is_file():
        info = path.stat()
        return {'path': str(path), 'kind': 'file', 'bytes': str(info.st_size),
                'allocated_bytes': str(getattr(info, 'st_blocks', 0) * 512 or info.st_size),
                'device': str(info.st_dev), 'inode': str(info.st_ino),
                'mtime_ns': str(info.st_mtime_ns), 'ctime_ns': str(info.st_ctime_ns)}
    if not path.is_dir():
        raise ValueError('allocated input must be a file or directory')
    total = allocated_total = 0
    files = 0
    for child in path.rglob('*'):
        if child.is_symlink():
            raise ValueError('allocated-input inventory does not follow symlinks')
        if child.is_file():
            child_stat = child.stat()
            total += child_stat.st_size
            allocated_total += getattr(child_stat, 'st_blocks', 0) * 512 or child_stat.st_size
            files += 1
    info = path.stat()
    return {'path': str(path), 'kind': 'directory', 'bytes': str(total),
            'allocated_bytes': str(allocated_total), 'files': str(files),
            'device': str(info.st_dev), 'inode': str(info.st_ino),
            'mtime_ns': str(info.st_mtime_ns), 'ctime_ns': str(info.st_ctime_ns)}


def assess(available, pg_bytes, canonical_bytes, facts_bytes, raw_delta_bytes,
           target_cap=16*GIB, reserve=4*GIB, growth_percent=20,
           phase='all', clock_output_cap=4*GIB, spool_cap=NATIVE_SPOOL_CAP_GIB*GIB,
           scope_cap=4*GIB, scratch_cap=GIB, evidence_cap=GIB):
    if min(available, pg_bytes, canonical_bytes, facts_bytes, raw_delta_bytes,
           target_cap, reserve, growth_percent, clock_output_cap, spool_cap,
           scope_cap, scratch_cap, evidence_cap) < 0:
        raise ValueError('budget values must be nonnegative')
    if phase not in ('all', 'final_inputs', 'fresh_facts', 'retained_reconciliation'):
        raise ValueError('unknown migration resource phase')
    grown = lambda size: (size * (100 + growth_percent) + 99) // 100
    projected_facts = grown(facts_bytes)
    # Current free space already reflects every preserved file and every actual
    # archive. Receipts describe inputs; they never enter this arithmetic.
    inputs = grown(pg_bytes) + grown(canonical_bytes) + clock_output_cap + raw_delta_bytes
    bounds = {
        'final_inputs': inputs + scratch_cap,
        'fresh_facts': target_cap,
        'retained_reconciliation': spool_cap + scope_cap + evidence_cap,
        'all': inputs + target_cap + max(scratch_cap, spool_cap + scope_cap + evidence_cap),
    }
    capped = bounds[phase]
    projected = inputs + projected_facts + max(scratch_cap, spool_cap + scope_cap + evidence_cap)
    return {
        'growth_percent': growth_percent, 'minimum_free_bytes': str(reserve),
        'target_file_cap_bytes': str(target_cap), 'estimated_new_facts_bytes': str(projected_facts),
        'estimated_peak_additional_bytes': str(projected),
        'estimated_required_available_bytes': str(projected + reserve),
        'target_cap_peak_additional_bytes': str(capped),
        'target_cap_required_available_bytes': str(capped + reserve),
        'estimated_margin_bytes': str(available - projected - reserve),
        'target_cap_margin_bytes': str(available - capped - reserve),
        'phase': phase,
        'phase_admitted': available >= capped + reserve and projected_facts <= target_cap,
        'ready_for_stop': phase == 'all' and available >= capped + reserve and projected_facts <= target_cap,
        'phase_additional_bounds_bytes': {name: str(size) for name, size in bounds.items()},
        'clock_output_cap_bytes': str(clock_output_cap), 'global_spool_cap_bytes': str(spool_cap),
        'single_scope_cap_bytes': str(scope_cap), 'reconciliation_evidence_cap_bytes': str(evidence_cap),
        'planner_and_independent_oracle_scratch_cap_bytes': str(scratch_cap),
        'archive_credit_bytes': '0', 'future_phase_requires_fresh_free_and_size_gate': True,
        'reuse_existing_generation_pages_proven': False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-directory', type=Path, required=True)
    parser.add_argument('--facts', type=Path, required=True)
    parser.add_argument('--target-volume', type=Path, required=True)
    parser.add_argument('--archived-input-receipt', type=Path)
    parser.add_argument('--allow-archived-base-source', action='store_true')
    parser.add_argument('--facts-kind', choices=('fresh', 'base'), default='fresh')
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--spool', type=Path)
    parser.add_argument('--scope-cache', type=Path)
    parser.add_argument('--reconciliation-evidence', type=Path)
    parser.add_argument('--raw-delta-budget-mib', type=int, default=1024)
    parser.add_argument('--growth-percent', type=int, default=20)
    parser.add_argument('--phase', choices=('all', 'final_inputs', 'fresh_facts', 'retained_reconciliation'), default='all')
    parser.add_argument('--target-cap-gib', type=int, default=16)
    parser.add_argument('--clock-output-cap-gib', type=int, default=4)
    parser.add_argument('--spool-cap-gib', type=int, default=NATIVE_SPOOL_CAP_GIB,
                        choices=(NATIVE_SPOOL_CAP_GIB,),
                        help='fixed native spool physical file cap in GiB')
    parser.add_argument('--scope-cap-gib', type=int, default=4)
    parser.add_argument('--scratch-cap-mib', type=int, default=1024)
    parser.add_argument('--reconciliation-evidence-cap-gib', type=int, default=1)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    source = args.source_directory.resolve(strict=True)
    manifest_path = source / 'manifest.json'
    manifest_bytes = stable_read(manifest_path)
    manifest = json.loads(manifest_bytes)
    receipt = validate_archive_receipt(args.archived_input_receipt) if args.archived_input_receipt else None
    retained_inputs = []
    for item in manifest.get('configs', []):
        retained_inputs.append(input_file(source / item['file'], 'source configuration',
                                          expected_sha=item['sha256']))
    raw_descriptor = manifest.get('raw') or {}
    if not raw_descriptor.get('file') or not raw_descriptor.get('sha256'):
        raise ValueError('source manifest must retain the complete original raw archive')
    retained_inputs.append(input_file(source / raw_descriptor['file'], 'retained raw archive',
                                      expected_sha=raw_descriptor['sha256']))
    native_mapping = raw_descriptor.get('native_mapping') or {}
    if not native_mapping.get('file') or not native_mapping.get('sha256'):
        raise ValueError('source manifest must retain its native raw mapping')
    retained_inputs.append(input_file(source / native_mapping['file'], 'retained native mapping',
                                      expected_sha=native_mapping['sha256']))
    if not isinstance(manifest.get('tables'), list) or not manifest['tables']:
        raise ValueError('source manifest does not enumerate PostgreSQL table inputs')
    tables = []
    for table in manifest['tables']:
        if not isinstance(table.get('file'), str) or Path(table['file']).name != table['file']:
            raise ValueError('source table filename must be a basename')
        path = source / table['file']
        observed = input_file(path, f"PostgreSQL table {table.get('table')}", receipt,
                              'postgres_table' if args.allow_archived_base_source else None,
                              table.get('sha256'))
        tables.append({'table': table['table'], 'rows': table['rows'],
                       'bytes': observed['bytes'], 'sha256': observed['sha256'],
                       'allocated_bytes': observed['allocated_bytes'],
                       'archived': observed['archived'], 'archive_group_id': observed.get('archive_group_id')})
    pg_bytes = sum(int(item['bytes']) for item in tables)
    descriptor_path = source / 'canonical-bars-v1.manifest.json'
    descriptor_hash = None
    canonical_record = None
    if descriptor_path.is_file() and not descriptor_path.is_symlink():
        descriptor_bytes = stable_read(descriptor_path)
        descriptor_hash = hashlib.sha256(descriptor_bytes).hexdigest()
        descriptor = json.loads(descriptor_bytes)
        canonical_record = {'file': descriptor.get('file'), 'sha256': descriptor.get('sha256')}
    if not canonical_record or not isinstance(canonical_record['file'], str) or Path(canonical_record['file']).name != canonical_record['file']:
        if not args.allow_archived_base_source:
            raise FileNotFoundError('fresh source canonical descriptor is required')
        raise ValueError('base canonical descriptor is preserved by policy; an archived body alone is supported')
    canonical_path = source / canonical_record['file']
    canonical = input_file(canonical_path, 'canonical input', receipt,
                           'canonical' if args.allow_archived_base_source else None,
                           canonical_record['sha256'])
    canonical_bytes = int(canonical['bytes'])
    facts = input_file(args.facts, 'facts input', receipt,
                       'facts' if args.facts_kind == 'base' else None)
    required_archive_groups = {item['archive_group_id'] for item in tables
                               if item.get('archived') and item.get('archive_group_id')}
    if canonical.get('archived') and canonical.get('archive_group_id'):
        required_archive_groups.add(canonical['archive_group_id'])
    verify_source_manifest_binding(receipt, manifest_path, manifest, descriptor_path,
                                   required_archive_groups)
    if facts.get('archived'):
        facts_group = next((group for group in receipt['groups']
                            if group['group_id'] == facts.get('archive_group_id')), None)
        native = ((facts_group or {}).get('source_binding') or {}).get('native_facts') or {}
        if native.get('source_path') != str(Path(args.facts).resolve()):
            raise ValueError('base facts archive native proof is bound to a different facts path')
    volume = args.target_volume.resolve(strict=True)
    available = shutil.disk_usage(volume).free
    if args.report.exists():
        raise FileExistsError('preflight evidence is immutable; choose a fresh report path')
    plan = assess(available, pg_bytes, canonical_bytes, int(facts['bytes']),
        args.raw_delta_budget_mib*1024**2, growth_percent=args.growth_percent,
        phase=args.phase, target_cap=args.target_cap_gib*GIB,
        clock_output_cap=args.clock_output_cap_gib*GIB, spool_cap=args.spool_cap_gib*GIB,
        scope_cap=args.scope_cap_gib*GIB, scratch_cap=args.scratch_cap_mib*1024**2,
        evidence_cap=args.reconciliation_evidence_cap_gib*GIB)
    report = {
        'kind': 'migration_peak_resource_preflight', 'schema': 'migration-space-preflight-v2',
        'read_only': True, 'sampled_at': datetime.now(timezone.utc).isoformat(),
        'source_manifest_id': manifest['id'],
        'source_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'retained_raw_source_inputs': retained_inputs,
        'canonical_descriptor_sha256': descriptor_hash,
        'current_available_bytes': str(available), 'postgres_table_files': tables,
        'observed_pg_export_bytes': str(pg_bytes), 'observed_canonical_source_bytes': str(canonical_bytes),
        'observed_existing_facts_bytes': facts['bytes'], 'facts_input': facts,
        'allocated_input_evidence': {
            'postgres_source': allocated(source), 'facts': allocated(args.facts) if args.facts.exists() else facts,
            'capture': allocated(args.capture, missing_ok=True),
            'decoded_spool': allocated(args.spool, missing_ok=True),
            'scope_cache': allocated(args.scope_cache), 'reconciliation_evidence': allocated(args.reconciliation_evidence),
            'target_volume': str(volume),
            'archive_storage': [group['archive'] for group in receipt['groups']] if receipt else []},
        'archived_input_receipt': ({'file': receipt['path'], 'sha256': receipt['sha256'],
                                    'groups': receipt['groups']} if receipt else None),
        'new_raw_delta_reserved_bytes': str(args.raw_delta_budget_mib*1024**2),
        'existing_source_raw_facts_candidate_preserved': True,
        'production_stopped': False, 'space_plan': plan,
        'limitations': [
            'Raw delta is an explicit reservation, not an observed future tail.',
            'Static final PG export, original canonical, corrected canonical/mapping/points and inactive facts coexist.',
            'Full retained spool and one dependency-complete scope cache coexist with their immutable inputs/facts.',
            'Planner scratch includes 512MiB clock planner and 512MiB independent audit; source selector scratch is bounded separately by the wrapper.',
            'Phase admission uses a fresh current filesystem free-space sample; every later phase needs its own gate.',
            'Archive receipts verify preserved source identities and actual archive bytes, but add no space credit.',
            'Stop requires the target-cap budget; passing does not authorize stop or prove drain/cutover.',
            'No same-file generation page reuse or rollback savings are assumed.']}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as out:
        json.dump(report, out, indent=2, sort_keys=True)
        out.write('\n'); out.flush(); os.fsync(out.fileno())
    print(json.dumps({'report': str(args.report.resolve()), 'ready_for_stop': plan['ready_for_stop'],
                      'phase_admitted': plan['phase_admitted'],
                      'target_cap_margin_bytes': plan['target_cap_margin_bytes']}))
    return 0 if plan['phase_admitted'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
