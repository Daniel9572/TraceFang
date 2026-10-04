#!/usr/bin/env python3
"""Phased terminal handoff. Stops/providers are controlled by the fleet, never by this tool."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import time


NATIVE_SPOOL_CAP_GIB = 5


def spool_child_limit():
    import resource
    cap = NATIVE_SPOOL_CAP_GIB * 1024**3
    resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))


def space_preflight_command(args, preflight_script, source_dir, facts_path,
                            phase, facts_kind, report_path):
    return [sys.executable, preflight_script, '--source-directory', source_dir,
            '--facts', facts_path, '--facts-kind', facts_kind,
            '--target-volume', args.capture.parent, '--phase', phase,
            '--target-cap-gib', str(args.facts_cap_gib), '--capture', args.capture,
            '--spool', args.spool, '--spool-cap-gib', str(args.spool_cap_gib),
            '--report', report_path]


def file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def stable_read(path, limit=64 * 1024 * 1024):
    path = Path(path)
    before = os.stat(path, follow_symlinks=False)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f'input must be a regular non-symlink file: {path}')
    chunks = []
    count = 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if file_identity(opened) != file_identity(before):
            raise ValueError(f'input changed before read: {path}')
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            count += len(block)
            if count > limit:
                raise ValueError(f'bounded evidence read exceeds {limit} bytes: {path}')
            chunks.append(block)
        after_fd = os.fstat(stream.fileno())
    after = os.stat(path, follow_symlinks=False)
    if (count != before.st_size or file_identity(after_fd) != file_identity(before)
            or file_identity(after) != file_identity(before)):
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
        if file_identity(opened) != file_identity(before):
            raise ValueError(f'input changed before hashing: {path}')
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            count += len(block)
        after_fd = os.fstat(stream.fileno())
    after = os.stat(path, follow_symlinks=False)
    if (count != before.st_size or file_identity(after_fd) != file_identity(before)
            or file_identity(after) != file_identity(before)):
        raise ValueError(f'input was short-hashed or changed during read: {path}')
    return digest.hexdigest()


def read(path):
    return json.loads(stable_read(path))


def save(path, value, replace=False):
    path = Path(path)
    if path.exists() and not replace:
        raise FileExistsError(f'authority evidence is immutable; refusing to overwrite {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.pending', dir=path.parent)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(value, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    if replace:
        os.replace(temporary, path)
    else:
        os.link(temporary, path)
        os.unlink(temporary)
    if os.name == 'posix':
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


SHA = re.compile(r'[0-9a-f]{64}\Z')
POLICY = 'legacy-bars-fixed-authority+clock-projection+retained-raw-overlay-v2'
CLASSIFICATION_POLICY = 'retained-envelope-http502-and-optional-daily-statistics-v1'
AUDIT_SCHEMA = 'independent-clock-projection-audit-v1'
MAPPING_SCHEMA = 'legacy-source-clock-projection-v2'


def require_sha(value, what):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError(f'{what} must be a lowercase SHA-256')
    return value


def artifact(path, digest=None):
    path = Path(path).resolve(strict=True)
    actual = sha(path)
    if digest is not None and actual != digest:
        raise ValueError(f'artifact SHA changed: {path}')
    return {'file': str(path), 'sha256': actual}


def verify_report_artifact(progress, record, json_rows=False):
    if not isinstance(record, dict) or record.get('complete') is not True:
        raise ValueError('reconciliation artifact is not complete')
    name = record.get('file')
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError('reconciliation artifacts must use progress-directory basenames')
    path = (Path(progress) / name).resolve(strict=True)
    if path.parent != Path(progress).resolve(strict=True):
        raise ValueError('reconciliation artifact escapes the progress directory')
    require_sha(record.get('sha256'), 'reconciliation artifact digest')
    if sha(path) != record['sha256']:
        raise ValueError(f'reconciliation artifact changed: {name}')
    count = 0
    if not json_rows:
        opener = gzip.open if path.name.endswith('.gz') else open
        expanded = 0
        with opener(path, 'rb') as stream:
            while True:
                line = stream.readline(4 * 1024 * 1024 + 1)
                if not line:
                    break
                expanded += len(line)
                if len(line) > 4 * 1024 * 1024 or expanded > 8 * 1024**3:
                    raise ValueError(f'compressed reconciliation evidence exceeds its decode bound: {name}')
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f'reconciliation ledger rows must be JSON objects: {name}')
                count += 1
    if not json_rows and int(record.get('row_count', -1)) != count:
        raise ValueError(f'reconciliation artifact row count differs: {name}')
    return path


def stop_input(stop):
    stop = Path(stop).resolve(strict=True)
    stopped = read(stop)
    if not (stopped['schema'] == 'legacy-stop-evidence-v1'
            and stopped['production_terminal'] is True
            and stopped['raw_producers_stopped'] is True
            and stopped['stopped_component_ids']
            and len(set(stopped['stopped_component_ids'])) == len(stopped['stopped_component_ids'])):
        raise ValueError('actual stopped-producer identity evidence required')
    inspection_path = Path(stopped.get('inspection_report_path', '')).resolve(strict=True)
    if inspection_path.parent != stop.parent:
        raise ValueError('earlier stop inspection must be retained beside its final receipt')
    require_sha(stopped.get('inspection_report_sha256'), 'earlier stop inspection SHA')
    if sha(inspection_path) != stopped['inspection_report_sha256']:
        raise ValueError('earlier stop inspection receipt changed')
    inspection = read(inspection_path)
    evidence = stopped.get('evidence') or {}
    for key, expected_name in (('inspection', inspection_path.name),
                               ('pre_stop_observation', 'pre-stop-observation.json'),
                               ('attempt', 'stop-attempt.json')):
        ref = evidence.get(key) or {}
        if ref.get('file') != expected_name or not SHA.fullmatch(str(ref.get('sha256', ''))):
            raise ValueError(f'legacy stop {key} evidence reference is malformed')
        item = (stop.parent / expected_name).resolve(strict=True)
        if item.parent != stop.parent or sha(item) != ref['sha256']:
            raise ValueError(f'legacy stop {key} evidence changed')
    if (inspection.get('schema') != 'legacy-installed-stop-inspection-v1'
            or inspection.get('processes_changed') is not False
            or inspection.get('plist_sha256') != stopped.get('plist_sha256')
            or inspection.get('domain') != stopped.get('domain')
            or inspection.get('registration') != stopped.get('inspection_registration')
            or inspection.get('processes') != stopped.get('inspection_processes')):
        raise ValueError('stop report is not bound to the independently inspected installed job/PID tree')
    plist_path = Path(inspection.get('plist_path', '')).resolve(strict=True)
    if (inspection.get('domain') != stopped.get('domain')
            or inspection.get('plist_sha256') != stopped.get('plist_sha256')
            or sha(plist_path) != stopped.get('plist_sha256')):
        raise ValueError('installed launchd domain/plist bytes differ from the earlier inspection')
    prebootout = stopped.get('prebootout')
    pre_stop = read(stop.parent / 'pre-stop-observation.json')
    attempt = read(stop.parent / 'stop-attempt.json')
    if not isinstance(prebootout, dict) or not (
            prebootout.get('plist_sha256') == stopped.get('plist_sha256')
            and prebootout.get('registration') == inspection.get('registration')
            and prebootout.get('processes') == inspection.get('processes')
            and int(prebootout.get('observed_at_ns', '0')) >= int(inspection['observed_at_ns'])
            and prebootout.get('bootout_exit_code') == 0
            and pre_stop.get('schema') == 'legacy-installed-prebootout-observation-v1'
            and pre_stop.get('observed_at_ns') == prebootout.get('observed_at_ns')
            and pre_stop.get('domain') == stopped.get('domain')
            and pre_stop.get('plist_sha256') == stopped.get('plist_sha256')
            and pre_stop.get('registration') == inspection.get('registration')
            and pre_stop.get('processes') == inspection.get('processes')
            and pre_stop.get('inspection_report_sha256') == stopped.get('inspection_report_sha256')
            and attempt.get('schema') == 'legacy-stop-attempt-v1'
            and attempt.get('bootout_exit_code') == 0
            and attempt.get('phase') == 'stopped_process_tree_and_listener_verified'
            and attempt.get('domain') == stopped.get('domain')
            and attempt.get('plist_sha256') == stopped.get('plist_sha256')
            and int(attempt.get('bootout_requested_at_ns', '0')) >= int(pre_stop.get('observed_at_ns', '0'))
            and int(attempt.get('bootout_completed_at_ns', '0')) >= int(attempt.get('bootout_requested_at_ns', '0'))
            and attempt.get('inspection_report_sha256') == stopped.get('inspection_report_sha256')
            and attempt.get('pre_stop_observation_sha256') == evidence['pre_stop_observation']['sha256']
            and attempt.get('native_providers_started') is False
            and attempt.get('process_tree_exited') is True
            and attempt.get('listener_closed') is True
            and attempt.get('stable_no_restart_observations') == stopped.get('stable_no_restart_observations')
            and int(attempt.get('verified_at_ns', '0')) >= int(attempt.get('bootout_completed_at_ns', '0'))
            and int(stopped.get('observed_at_ns', '0')) >= int(attempt.get('verified_at_ns', '0'))
            and int(stopped.get('observed_at_ns', '0')) >= int(attempt.get('bootout_completed_at_ns', '0'))
            and stopped.get('process_tree_exited') is True
            and stopped.get('listener_closed') is True
            and stopped.get('no_resurrection') is True):
        raise ValueError('fresh prebootout, successful bootout, process exit and no-resurrection evidence required')
    observations = stopped.get('stable_no_restart_observations')
    if (not isinstance(observations, list) or len(observations) < 2
            or any(row.get('job_registered') is not False or row.get('ready_endpoint_closed') is not True
                   or row.get('process_tree_exited') is not True for row in observations)
            or int(observations[-1].get('observed_at_ns', '0')) - int(observations[0].get('observed_at_ns', '0'))
               < int(float(stopped.get('stable_interval_seconds', 0)) * 1_000_000_000)):
        raise ValueError('stable closed-port and no-resurrection observations are required')
    require_sha(stopped.get('plist_sha256'), 'installed plist SHA')
    if not stopped.get('plist_preserved'):
        raise ValueError('the independently inspected launchd plist was not preserved')
    return stopped


def closure_inputs(stop, drain, reconciliation):
    stopped, drained = stop_input(stop), read(drain)
    if not (drained.get('schema') == 'legacy-drain-evidence-v1'
            and drained.get('production_terminal') is True
            and int(drained.get('unresolved_frames', -1)) == 0
            and drained.get('method') == 'independent_retained_raw_reconciliation'
            and drained.get('raw_applied_through_legacy') is None
            and reconciliation is not None
            and drained.get('reconciliation_report_sha256') == sha(reconciliation)):
        raise ValueError('independent retained-raw reconciliation is required; old writer exit/ack is insufficient')
    return stopped, drained


def resolve_artifact(directory, record, expected_name):
    if not isinstance(record, dict) or record.get('file') != expected_name:
        raise ValueError(f'contract artifact must be named {expected_name}')
    name = Path(record['file'])
    if name.name != expected_name or not SHA.fullmatch(str(record.get('sha256', ''))):
        raise ValueError(f'invalid {expected_name} artifact reference')
    path = (Path(directory) / expected_name).resolve(strict=True)
    if path.parent != Path(directory).resolve(strict=True) or sha(path) != record['sha256']:
        raise ValueError(f'{expected_name} does not match its SHA-bound progress copy')
    return path


def validate_clock_inputs(source, clock_dir, audit_path):
    source = Path(source).resolve(strict=True)
    clock_dir = Path(clock_dir).resolve(strict=True)
    manifest = read(source / 'manifest.json')
    original = read(source / 'canonical-bars-v1.manifest.json')
    mapping_path = clock_dir / 'canonical-bars-clock-v2.manifest.json'
    mapping = read(mapping_path)
    mapping_file = mapping.get('file')
    if not isinstance(mapping_file, str) or Path(mapping_file).name != mapping_file:
        raise ValueError('clock projection output must be a source-directory basename')
    corrected_path = clock_dir / mapping_file
    audit = read(audit_path)
    counts = mapping.get('counts') or {}
    projected_rows = int(counts.get('output_rows', '-1'))
    point_rows = int(counts.get('point_rows', '-1'))
    collided_rows = int(counts.get('collided_input_rows', '-1'))
    input_rows = int(counts.get('input_rows', '-1'))
    if not (mapping.get('schema') == MAPPING_SCHEMA and mapping.get('complete') is True
            and mapping.get('activation') is False
            and mapping.get('source_manifest_id') == manifest['id']
            and mapping.get('snapshot') == manifest['postgres']['snapshot']
            and mapping.get('source_fingerprint') == manifest['postgres']['fingerprint']
            and mapping.get('original_descriptor_sha256') == sha(source / 'canonical-bars-v1.manifest.json')
            and mapping.get('original_canonical_file_sha256') == original['sha256']
            and mapping.get('policy_version') == 'legacy-bars-fixed-authority+clock-projection-v2'
            and mapping.get('policy', {}).get('policy_version') == 'ths-v6-period61-shfe-interval-end-v2'
            and counts.get('unresolved_differences') == '0'
            and min(projected_rows, point_rows, collided_rows, input_rows) >= 0
            and input_rows == projected_rows + point_rows + collided_rows):
        raise ValueError('clock mapping is incomplete or belongs to a different fresh PG snapshot')
    if sha(corrected_path) != mapping.get('sha256'):
        raise ValueError('corrected clock canonical changed')
    for item in mapping.get('artifacts', {}).values():
        if (not isinstance(item, dict) or not isinstance(item.get('file'), str)
                or Path(item['file']).name != item['file']
                or not SHA.fullmatch(str(item.get('sha256', '')))
                or sha(clock_dir / item['file']) != item['sha256']):
            raise ValueError('clock mapping evidence artifact changed')
    if not (audit.get('schema') == AUDIT_SCHEMA and audit.get('complete') is True
            and audit.get('policy') == 'ths-v6-period61-shfe-interval-end-v2'
            and audit.get('original_file_sha256') == original['sha256']
            and audit.get('projected_file_sha256') == mapping['sha256']
            and audit.get('projection_manifest_sha256') == sha(mapping_path)):
        raise ValueError('independent root clock auditor did not certify these exact fresh clock inputs')
    return manifest, original, mapping, audit, mapping_path, corrected_path


def verify_seal(seal_path, expected_sha=None):
    seal_path = Path(seal_path).resolve(strict=True)
    if expected_sha and sha(seal_path) != expected_sha:
        raise ValueError('frozen tool seal changed')
    seal = read(seal_path)
    if not (seal.get('schema') == 'tracefang-migration-tools-v1'
            and seal.get('complete') is True
            and seal.get('conflict_policy_version') == POLICY):
        raise ValueError('complete v3 migration tool seal required')
    build = require_sha(seal.get('backend_build_sha256'), 'whole backend build fingerprint')
    if not isinstance(seal.get('files'), list) or not seal['files']:
        raise ValueError('seal must list the complete immutable inputs')
    sealed = {}
    roles = {}
    for item in seal['files']:
        if not isinstance(item, dict):
            raise ValueError('sealed input entries must be objects')
        path_text = item.get('path')
        digest = require_sha(item.get('sha256'), 'sealed input SHA')
        raw_path = Path(path_text)
        if raw_path.is_symlink():
            raise ValueError('sealed input path cannot be a symlink')
        path = raw_path.resolve(strict=True)
        if not path.is_file() or sha(path) != digest:
            raise ValueError('sealed migration input changed: ' + str(path))
        if any(part in ('rust-target', '.rust-target') for part in path.parts) or (
                len(path.parts) > 1 and path.parts[-2] == 'target'):
            raise ValueError('terminal executable cannot come from mutable Cargo target')
        if path in sealed:
            raise ValueError('seal contains duplicate input paths')
        sealed[path] = digest
        roles.setdefault(item.get('role'), []).append((path, item))
    runtime = seal.get('runtime') or {}
    runtime_path = Path(sys.executable).resolve(strict=True)
    if (Path(runtime.get('executable', '')).resolve(strict=True) != runtime_path
            or runtime.get('sha256') != sha(runtime_path)
            or runtime_path not in sealed
            or sealed[runtime_path] != runtime.get('sha256')):
        raise ValueError('active Python interpreter differs from the sealed runtime authority')
    if (len(roles.get('python_runtime', [])) != 1
            or runtime.get('version') != sys.version
            or runtime.get('implementation') != sys.implementation.name):
        raise ValueError('seal must contain one Python runtime authority')
    executables = seal.get('executables')
    if not isinstance(executables, dict) or set(executables) != {'probe', 'reconcile-probe', 'clock-probe', 'spool-probe'}:
        raise ValueError('seal must bind the four named Release helpers exactly')
    for name, path in executables.items():
        resolved = Path(path).resolve(strict=True)
        if resolved not in sealed or sealed[resolved] != sha(resolved):
            raise ValueError('copied immutable Release helper is absent from the seal: ' + name)
        if stat.S_IMODE(resolved.stat().st_mode) & 0o222:
            raise ValueError('copied Release helper is writable: ' + str(resolved))
        if not os.access(resolved, os.X_OK):
            raise ValueError('copied Release helper is not executable: ' + str(resolved))
        record = [item for candidate, item in roles.get('release_executable', [])
                  if candidate == resolved and item.get('name') == name]
        if len(record) != 1:
            raise ValueError('copied helper role/name differs from the sealed executable map')
        try:
            resolved.relative_to(Path(seal['immutable_helper_directory']).resolve(strict=True))
        except ValueError as error:
            raise ValueError('Release helper is outside the immutable helper directory') from error
    policy = seal.get('clock_policy') or {}
    if (policy.get('conflict_policy_version') != POLICY
            or policy.get('policy_id') != 'ths-v6-period61-shfe-interval-end-v2'):
        raise ValueError('sealed clock policy differs from the v3 boundary policy')
    for role, expected in (('clock_policy_source', policy.get('source_sha256')),
                           ('independent_clock_auditor', policy.get('auditor_sha256'))):
        files = roles.get(role, [])
        if len(files) != 1 or files[0][1]['sha256'] != expected:
            raise ValueError(f'seal does not bind one exact {role}')
    witnesses = policy.get('witnesses')
    if not isinstance(witnesses, list) or not witnesses:
        raise ValueError('sealed clock policy witnesses are required')
    for witness in witnesses:
        path = Path(witness['file']).resolve(strict=True)
        if path not in sealed or sealed[path] != witness['sha256']:
            raise ValueError('clock witness is not bound by the seal')
    for required in ('probe', 'reconcile-probe', 'clock-probe', 'spool-probe'):
        path = Path(seal.get('executables', {}).get(required, '')).resolve(strict=True)
        if path not in sealed:
            raise ValueError('sealed Release executor missing: ' + required)
    script_records = roles.get('script', [])
    script_names = {item.get('name') for _path, item in script_records}
    if len(script_records) != 7 or script_names != {
            'migration-terminal-handoff.py', 'migration-space-preflight.py',
            'legacy-stop-evidence.py', 'prepare-legacy-canonical-bars.py',
            'prepare-legacy-clock-canonical.py', 'prepare-clock-series-state.py',
            'seal-migration-tools.py'}:
        raise ValueError('seal does not bind all exact terminal scripts and the seal builder')
    source_root, expected_sources = backend_source_inputs(seal.get('backend_source_root', ''))
    source_records = roles.get('backend_source', [])
    expected_names = {path.relative_to(source_root).as_posix() for path in expected_sources}
    actual_names = {item.get('name') for _path, item in source_records}
    if (not source_records or actual_names != expected_names
            or len(source_records) != len(expected_names)):
        raise ValueError('seal does not bind the complete build.rs backend source closure')
    for path in expected_sources:
        relative = path.relative_to(source_root).as_posix()
        matches = [(candidate, item) for candidate, item in source_records if item.get('name') == relative]
        if (len(matches) != 1 or matches[0][0] != path
                or matches[0][1].get('sha256') != sha(path)):
            raise ValueError(f'backend source closure changed or is incomplete: {relative}')
    if seal.get('backend_source_closure_sha256') != backend_source_closure_sha256(
            [item for _path, item in source_records]):
        raise ValueError('backend source closure digest differs from the sealed source list')
    identity = seal.get('identity') or {}
    if (identity.get('backend_build_sha256') != build
            or identity.get('version') != seal.get('backend_version')
            or not isinstance(seal.get('backend_version'), str)):
        raise ValueError('seal identity/backend version aliases differ')
    return seal, sealed, roles


def verify_execution(command, sealed):
    if not command:
        raise ValueError('empty command')
    executable = Path(str(command[0])).resolve(strict=True)
    if executable not in sealed or sha(executable) != sealed[executable]:
        raise ValueError('execution would use an unsealed/changed executable: ' + str(executable))
    # Path(command[0]) is frequently compared to a string from argparse. Always
    # resolve the interpreter and script explicitly before dispatch.
    if executable == Path(sys.executable).resolve(strict=True):
        if len(command) < 2:
            raise ValueError('Python invocation lacks a sealed script')
        script = Path(str(command[1])).resolve(strict=True)
        if script not in sealed or sha(script) != sealed[script]:
            raise ValueError('execution would use an unsealed/changed Python script: ' + str(script))
    for part in command[1:]:
        path = Path(str(part))
        if path.suffix in ('.py', '.rs') and path.is_file():
            path = path.resolve(strict=True)
            if path not in sealed or sha(path) != sealed[path]:
                raise ValueError('execution would use an unsealed/changed Python input: ' + str(path))


def verify_source_snapshot(source, archive_receipt=None, allow_archived=False,
                           require_mapping=True):
    source = Path(source).resolve(strict=True)
    manifest_path = source / 'manifest.json'
    manifest = read(manifest_path)
    pins = {manifest_path: sha(manifest_path)}
    descriptor_path = source / 'canonical-bars-v1.manifest.json'

    def require_group_current_source(group_id, canonical=False):
        group = next((item for item in (archive_receipt or {}).get('groups', [])
                      if item.get('group_id') == group_id), None)
        binding = (group or {}).get('source_binding') or {}
        source_ref = binding.get('source_manifest') or {}
        if (source_ref.get('file') != str(manifest_path.resolve(strict=True))
                or source_ref.get('sha256') != pins[manifest_path]
                or binding.get('source_manifest_id') != manifest.get('id')
                or binding.get('postgres_snapshot') != (manifest.get('postgres') or {}).get('snapshot')):
            raise ValueError('named archived PG/canonical body is bound to a different source manifest/snapshot')
        if canonical:
            descriptor_ref = binding.get('canonical_descriptor') or {}
            if (not descriptor_path.is_file() or descriptor_path.is_symlink()
                    or descriptor_ref.get('file') != str(descriptor_path.resolve(strict=True))
                    or descriptor_ref.get('sha256') != sha(descriptor_path)):
                raise ValueError('named canonical archive is bound to another source descriptor')

    def add(relative, digest, group_kind=None):
        if not isinstance(relative, str) or Path(relative).name != relative:
            raise ValueError('source manifest artifact escapes immutable source directory')
        path = source / relative
        if path.is_file() and not path.is_symlink():
            resolved = path.resolve(strict=True)
            if resolved.parent != source or sha(resolved) != digest:
                raise ValueError('source manifest artifact changed: ' + str(path))
            pins[resolved] = digest
            return
        if not allow_archived or group_kind is None or path.exists() or path.is_symlink():
            raise ValueError('required source artifact is missing: ' + str(path))
        archived = (archive_receipt or {}).get('files', {}).get(str(path.resolve()))
        allowed = {'postgres_table': {'retired-original-pg-table-pairs'},
                   'canonical': {'retired-original-canonical'}}.get(group_kind, set())
        if archived is None or archived.get('sha256') != digest or archived.get('group_id') not in allowed:
            raise ValueError('missing base input lacks its exact named archive receipt: ' + str(path))
        if group_kind == 'postgres_table':
            require_group_current_source(archived['group_id'])
        elif group_kind == 'canonical':
            require_group_current_source(archived['group_id'], canonical=True)
    for item in manifest.get('tables', []):
        add(item['file'], item['sha256'], 'postgres_table' if allow_archived else None)
    for item in manifest.get('configs', []):
        add(item['file'], item['sha256'])
    raw = manifest.get('raw', {})
    if not raw.get('file') or not raw.get('sha256'):
        raise ValueError('source manifest must preserve the original raw archive')
    add(raw['file'], raw['sha256'])
    mapping = raw.get('native_mapping') or {}
    if not mapping.get('file') or not mapping.get('sha256'):
        if require_mapping or mapping:
            raise ValueError('source manifest must preserve its native raw mapping')
        base = raw.get('incremental_base') or {}
        if (manifest.get('state') != 'fixed_inputs_exported'
                or raw.get('state') != 'fixed_range_exported'
                or not base.get('source_manifest_path') or not base.get('source_manifest_sha256')):
            raise ValueError('only an explicit terminal export may precede native raw mapping')
        base_path = Path(base['source_manifest_path']).resolve(strict=True)
        if sha(base_path) != require_sha(base['source_manifest_sha256'], 'incremental base manifest SHA'):
            raise ValueError('incremental raw base manifest changed')
        base_manifest = read(base_path)
        base_raw = base_manifest.get('raw') or {}
        if (base_manifest.get('id') != base.get('source_manifest_id')
                or base_raw != base.get('raw')):
            raise ValueError('terminal export embeds a different incremental raw base')
        pins[base_path] = base['source_manifest_sha256']
        for record in (base_raw, base_raw.get('native_mapping') or {}):
            name = record.get('file')
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError('incremental raw base artifact must be an immutable basename')
            path = base_path.parent / name
            if path.is_symlink() or sha(path) != require_sha(record.get('sha256'), 'incremental raw base artifact SHA'):
                raise ValueError('incremental raw base archive/mapping changed')
            pins[path.resolve(strict=True)] = record['sha256']
    else:
        add(mapping['file'], mapping['sha256'])
    descriptor = descriptor_path
    if descriptor.exists():
        pins[descriptor] = sha(descriptor)
        canonical = read(descriptor)
        add(canonical['file'], canonical['sha256'], 'canonical' if allow_archived else None)
    elif allow_archived:
        raise ValueError('base canonical descriptor must remain available for archive resolution')
    return manifest, pins


def verify_pins(pins):
    for path, expected in pins.items():
        if sha(path) != expected:
            raise ValueError('input changed before execution: ' + str(path))


def verify_export_resume(args, seal):
    evidence = args.resume_export_evidence.resolve(strict=True)
    prior_journal = evidence / 'handoff-journal.json'
    prior = read(prior_journal)
    if (prior.get('schema') != 'terminal-handoff-journal-v1'
            or any(prior.get(key) != str(getattr(args, argument)) for key, argument in
                   [('source_path', 'source'), ('facts_path', 'facts'), ('capture_path', 'capture'),
                    ('progress_path', 'progress_directory'), ('clock_path', 'clock_directory')])
            or prior.get('old_services_changed_by_this_tool') is not False
            or prior.get('providers_started_by_this_tool') is not False):
        raise ValueError('export checkpoint belongs to another target or service operation')
    completed = [row for row in prior.get('events', []) if row.get('state') == 'complete']
    if ([row.get('phase') for row in completed]
            != ['space-preflight-inputs', 'tail-before', 'terminal-export']
            or any(row.get('exit_code') != 0 for row in completed)
            or any(row.get('phase') not in ('space-preflight-inputs', 'tail-before',
                    'terminal-export', 'prepare-terminal-inputs') for row in prior.get('events', []))):
        raise ValueError('only the completed export checkpoint may be resumed')
    old_seal_path = args.resume_tools_manifest.resolve(strict=True)
    old_sha = sha(old_seal_path)
    if old_sha != prior.get('tools_manifest_sha256'):
        raise ValueError('export checkpoint tool seal changed')
    old_seal, _old_pins, _old_roles = verify_seal(old_seal_path, old_sha)
    if (old_seal['backend_build_sha256'] != seal['backend_build_sha256']
            or set(old_seal['executables']) != set(seal['executables'])
            or any(sha(old_seal['executables'][name]) != sha(path)
                   for name, path in seal['executables'].items())):
        raise ValueError('export resume must retain the exact compiled core and executors')
    if (args.source / 'canonical-bars-v1.manifest.json').exists():
        raise ValueError('export-only checkpoint cannot resume a later source transformation')
    manifest, _pins = verify_source_snapshot(args.source, require_mapping=False)
    tail_path = evidence / 'tail-before.json'
    tail = read(tail_path)
    after = manifest['raw'].get('incremental_after') or {}
    if (tail.get('stream') != after.get('stream') or tail.get('epoch') != after.get('epoch')
            or str(tail.get('last_sequence')) != str(after.get('sequence'))):
        raise ValueError('export checkpoint tail does not match its completed fixed raw range')
    logs = []
    for row in completed:
        path = Path(row['log']).resolve(strict=True)
        if path.parent != evidence:
            raise ValueError('export checkpoint log escapes its preserved evidence directory')
        logs.append({'file': str(path), 'sha256': sha(path)})
    return tail_path, {'journal': {'file': str(prior_journal), 'sha256': sha(prior_journal)},
                      'tools_manifest': {'file': str(old_seal_path), 'sha256': old_sha},
                      'source_manifest_sha256': sha(args.source / 'manifest.json'), 'logs': logs}


def clone_capture_snapshot(source, destination):
    """Clone a quiescent capture without changing its native epoch or source."""
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_file() or destination.exists() or destination.is_symlink():
        raise ValueError('base capture must be regular and its independent destination fresh')
    source = source.resolve(strict=True)
    handles = subprocess.run(['lsof', str(source)], capture_output=True)
    if handles.returncode != 1 or handles.stdout:
        raise ValueError('base capture must have no open handles before snapshot')
    before = source.stat()
    source_sha = sha(source)
    copied = subprocess.run(['/bin/cp', '-c', str(source), str(destination)],
                            capture_output=True)
    method = 'apfs_clone'
    if copied.returncode:
        if shutil.disk_usage(destination.parent).free < before.st_size + 4*1024**3:
            raise RuntimeError('independent capture copy would breach the 4GiB free floor; preserve all inputs')
        shutil.copyfile(source, destination)
        method = 'independent_copy'
    with destination.open('rb') as stream:
        os.fsync(stream.fileno())
    after = source.stat()
    stable = lambda value: (*file_identity(value), value.st_ctime_ns, value.st_nlink)
    if (stable(before) != stable(after) or sha(source) != source_sha
            or sha(destination) != source_sha or destination.stat().st_ino == after.st_ino):
        raise ValueError('base capture snapshot/source identity or complete bytes changed')
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {'source': str(source), 'destination': str(destination), 'sha256': source_sha, 'method': method,
            'bytes': str(before.st_size), 'source_device': str(before.st_dev),
            'source_inode': str(before.st_ino), 'source_mtime_ns': str(before.st_mtime_ns),
            'source_ctime_ns': str(before.st_ctime_ns),
            'destination_inode': str(destination.stat().st_ino),
            'source_unchanged': True, 'all_handles_closed': True, 'native_epoch_preserved_by_exact_bytes': True}


def native_spool_through(manifest, early):
    raw = manifest.get('raw') or {}
    if (early.get('stream') != raw.get('stream') or early.get('epoch') != raw.get('epoch')
            or str(early.get('last_sequence')) != str((raw.get('incremental_after') or {}).get('sequence'))):
        raise ValueError('early tail differs from the exact raw stream/epoch/range in the fresh export')
    position = (raw.get('native_mapping') or {}).get('last_position') or {}
    value = str(position.get('sequence', ''))
    if not value.isdecimal() or not 0 < int(value) <= 2**64-1 or not position.get('epoch'):
        raise ValueError('decoded spool requires the imported native capture boundary')
    require_sha(position.get('digest'), 'native capture boundary digest')
    return value


def load_archive_receipt(path, preflight_namespace):
    if path is None:
        return None
    validator = preflight_namespace.get('validate_archive_receipt')
    if not callable(validator):
        raise RuntimeError('sealed preflight script does not expose archive receipt validation')
    return validator(path)


def copy_immutable(source, destination, expected_sha):
    source = Path(source)
    if source.is_symlink():
        raise ValueError(f'copy source cannot be a symlink: {source}')
    source = source.resolve(strict=True)
    before = os.stat(source, follow_symlinks=False)
    if sha(source) != expected_sha:
        raise ValueError('source artifact changed before sealing its progress copy')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o400)
    digest = hashlib.sha256()
    copied_bytes = 0
    with source.open('rb') as incoming, os.fdopen(descriptor, 'wb') as output:
        opened = os.fstat(incoming.fileno())
        if file_identity(opened) != file_identity(before):
            raise ValueError('copy source changed before open')
        for block in iter(lambda: incoming.read(1024 * 1024), b''):
            output.write(block); digest.update(block); copied_bytes += len(block)
        after_fd = os.fstat(incoming.fileno())
        output.flush(); os.fsync(output.fileno())
    os.chmod(destination, 0o400)
    after = os.stat(source, follow_symlinks=False)
    if (copied_bytes != before.st_size or file_identity(after_fd) != file_identity(before)
            or file_identity(after) != file_identity(before)
            or digest.hexdigest() != expected_sha):
        raise ValueError('progress copy does not match its source artifact')
    return destination.resolve(strict=True)


def backend_source_inputs(root):
    """Reconstruct the exact path set byte-included by backend/build.rs."""
    root = Path(root).resolve(strict=True)
    paths = []
    for relative_root in ('src', 'assets'):
        current = root/relative_root
        if not current.is_dir() or current.is_symlink():
            raise ValueError(f'sealed backend source directory is missing or linked: {current}')
        for parent, directories, names in os.walk(current, followlinks=False):
            base = Path(parent)
            if any((base/name).is_symlink() for name in directories):
                raise ValueError(f'sealed backend source tree contains a linked directory: {base}')
            directories.sort()
            for name in sorted(names):
                path = base/name
                if path.is_symlink():
                    raise ValueError(f'sealed backend source tree contains a linked file: {path}')
                if path.suffix in ('.rs', '.json', '.sql'):
                    paths.append(path.resolve(strict=True))
    for relative in ('schema.sql', 'Cargo.toml', 'Cargo.lock', 'build.rs'):
        path = root/relative
        if relative == 'schema.sql' and not path.exists():
            continue
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'sealed backend build input is missing or linked: {path}')
        paths.append(path.resolve(strict=True))
    return root, sorted(set(paths), key=lambda path: path.relative_to(root).as_posix())


def backend_source_closure_sha256(records):
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item['name']):
        name = record['name'].encode('utf-8')
        digest.update(len(name).to_bytes(8, 'big'))
        digest.update(name)
        digest.update(bytes.fromhex(record['sha256']))
    return digest.hexdigest()


def validate_reconciliation(report_path, source_manifest, fixed_input_sha, clock_manifest,
                            original_source_sha, clock_audit_sha, seal, executor_sha,
                            spool_audit, progress, clock_artifacts):
    report_path = Path(report_path).resolve(strict=True)
    report = read(report_path)
    if not (report.get('schema') == 'legacy-reconciliation-v3'
            and report.get('complete') is True
            and report.get('production_terminal') is True
            and report.get('conflict_policy_version') == POLICY
            and report.get('unresolved_differences') == '0'
            and report.get('quote_events_reconciled') is True
            and report.get('initial_seed') == 'empty'
            and report.get('all_scopes_complete') is True):
        raise ValueError('v3 reconciliation report does not prove a complete terminal composite')
    build = seal['backend_build_sha256']
    if report.get('backend_build_sha256') != build or report.get('backend_build_fingerprint') != build:
        raise ValueError('reconciliation backend fingerprint aliases do not match sealed whole backend')
    if report.get('classification_policy') != CLASSIFICATION_POLICY:
        raise ValueError('decode rejection classification policy differs from the terminal contract')
    if report.get('fixed_input_manifest_sha256') != fixed_input_sha:
        raise ValueError('executor input source SHA must bind fixed-input-manifest.json')
    if report.get('postgres_snapshot') != source_manifest['postgres']['snapshot']:
        raise ValueError('reconciliation report belongs to another fresh PG snapshot')
    if report.get('source_manifest_id') != source_manifest['id']:
        raise ValueError('reconciliation report belongs to another fresh source manifest')
    binding = report.get('clock_binding') or {}
    refs = report.get('clock_projection') or {}
    if not (binding.get('source_manifest_id') == source_manifest['id']
            and binding.get('postgres_snapshot') == source_manifest['postgres']['snapshot']
            and binding.get('postgres_source_fingerprint') == source_manifest['postgres']['fingerprint']
            and binding.get('policy_id') == 'ths-v6-period61-shfe-interval-end-v2'
            and binding.get('original_source_manifest_sha256') == original_source_sha
            and binding.get('original_canonical_sha256') == clock_manifest['original_canonical_file_sha256']
            and binding.get('corrected_canonical_sha256') == clock_manifest['sha256']
            and refs.get('manifest') == binding.get('mapping_manifest')
            and refs.get('independent_audit') == binding.get('independent_audit')):
        raise ValueError('clock mapping/audit aliases do not bind the fresh PG source')
    mapping_ref = binding.get('mapping_manifest') or {}
    audit_ref = binding.get('independent_audit') or {}
    if (mapping_ref.get('file') != 'terminal-clock-manifest.json'
            or mapping_ref.get('sha256') != sha(clock_manifest['_path'])
            or audit_ref.get('file') != 'terminal-clock-audit.json'
            or audit_ref.get('sha256') != clock_audit_sha):
        raise ValueError('clock audit references must use the verified progress copies')
    sealed_witnesses = [(Path(item['file']).name, item['sha256']) for item in seal['clock_policy']['witnesses']]
    report_witnesses = [(Path(item['file']).name, item['sha256']) for item in binding.get('policy_witnesses', [])]
    if (binding.get('backend_build_sha256') != build
            or binding.get('policy_source_sha256') != seal['clock_policy']['source_sha256']
            or binding.get('auditor_sha256') != seal['clock_policy']['auditor_sha256']
            or report_witnesses != sealed_witnesses):
        raise ValueError('clock policy/auditor/build inputs differ from immutable seal')
    projection = report.get('clock_projection') or {}
    for key, expected in clock_artifacts.items():
        got = projection.get(key)
        if not isinstance(got, dict) or any(got.get(field) != expected[field] for field in ('file', 'sha256')):
            raise ValueError(f'clock projection {key} copy/reference differs from sealed input')
    if not isinstance(projection.get('policy_evidence'), list) or [
            (item.get('file'), item.get('sha256')) for item in projection['policy_evidence']
    ] != [(item['file'], item['sha256']) for item in clock_artifacts['policy_evidence']]:
        raise ValueError('clock projection witness copies differ from the seal')
    sealed_ref = report.get('sealed_tools') or {}
    if not (sealed_ref.get('file') == 'terminal-tools-manifest.json'
            and sealed_ref.get('sha256') == seal['_progress_copy_sha256']
            and sealed_ref.get('backend_build_sha256') == build
            and sealed_ref.get('executor_sha256') == executor_sha):
        raise ValueError('report is not bound to the copied release seal and selected executor')
    prefix = report.get('capture_prefix') or {}
    audit = spool_audit
    def sequence(value):
        if isinstance(value, dict):
            value = value.get('sequence', -1)
        return int(value)
    first = sequence(prefix.get('first', 0)); last = sequence(prefix.get('last', 0))
    frames = int(prefix.get('frames', '-1'))
    audit_ref = prefix.get('spool_audit') or {}
    if not (first == 1 and last > 0 and frames == last
            and prefix.get('original_prefix_complete') is False
            and prefix.get('origin_coverage', {}).get('original_prefix_complete') is False
            and audit_ref.get('file') == 'terminal-spool-audit.json'
            and audit_ref.get('sha256') == audit['_progress_copy_sha256']
            and prefix.get('spool_file_sha256') == audit.get('spool_file_sha256')):
        raise ValueError('retained native prefix/spool audit is incomplete or not bound to its fixed range')
    if prefix.get('canonical_decoded_sha256') != audit.get('manifest', {}).get('canonical_decoded_sha256'):
        raise ValueError('spool canonical decoded digest differs from report')
    lineage_record = prefix.get('raw_lineage')
    lineage_path = verify_report_artifact(progress, lineage_record, json_rows=True)
    lineage = read(lineage_path)
    if not (lineage.get('schema') == 'legacy-terminal-raw-lineage-v1'
            and lineage.get('complete') is True
            and lineage.get('fixed_input_manifest_sha256') == fixed_input_sha
            and sequence(lineage.get('first_native_sequence', 0)) == 1
            and sequence(lineage.get('last_position')) == last
            and int(lineage.get('frames', '-1')) == last
            and lineage.get('original_prefix_complete') is False
            and lineage.get('dependencies')):
        raise ValueError('full original-to-native raw lineage is incomplete or bound to different inputs')
    dependencies = lineage['dependencies']
    next_sequence = 1
    for dependency in dependencies:
        dep_first = sequence(dependency.get('first_native_sequence', 0))
        dep_last = sequence(dependency.get('last_position', dependency.get('last', 0)))
        dep_frames = int(dependency.get('frames', '-1'))
        proof = dependency.get('proof') or {}
        if (dep_first != next_sequence or dep_last < dep_first
                or dep_frames != dep_last - dep_first + 1
                or not SHA.fullmatch(str(dependency.get('raw_archive_sha256', '')))
                or not SHA.fullmatch(str(dependency.get('mapping_sha256', '')))
                or not SHA.fullmatch(str(dependency.get('source_manifest_sha256', '')))
                or proof.get('phase') != 'raw_native_verify'
                or proof.get('state') != 'complete_reopened_every_envelope'
                or int(proof.get('frames', '-1')) != dep_frames
                or sequence(proof.get('verified_first_sequence', 0)) != dep_first
                or sequence(proof.get('verified_last_position', 0)) != dep_last
                or proof.get('mapping_sha256') != dependency.get('mapping_sha256')):
            raise ValueError('raw lineage dependency lacks complete envelope/mapping verification to the exported tail')
        next_sequence = dep_last + 1
    if next_sequence != last + 1:
        raise ValueError('raw lineage dependencies do not form a gap-free 1..tail envelope chain')
    accounting = report.get('frame_accounting') or {}
    counted = [int(accounting.get(key, '-1')) for key in
               ('projection_frames', 'no_output_frames', 'classified_rejection_frames', 'unresolved_frames')]
    if (min(counted) < 0 or sum(counted) != last or counted[3] != 0
            or int(report.get('raw_frames_scanned', '-1')) != last):
        raise ValueError('four disjoint frame-accounting classes must exactly cover the raw prefix')
    ranges = report.get('affected_ranges')
    if not isinstance(ranges, list) or not ranges:
        raise ValueError('affected scope accounting is required')
    for scope in ranges:
        if (scope.get('complete') is not True or int(scope.get('raw_frames_scanned', '-1')) != last
                or int(scope.get('unresolved_differences', '-1')) != 0):
            raise ValueError('an affected scope lacks full-tail complete reconciliation')
    rejection = report.get('decode_rejections') or {}
    rejection_path = verify_report_artifact(progress, rejection)
    if int(rejection.get('unresolved', '-1')) != 0 or int(rejection.get('classified', '-1')) != counted[2]:
        raise ValueError('decode rejections are unclassified or disagree with frame accounting')
    classified = report.get('classified_rejections') or {}
    if any(classified.get(key) != rejection.get(key) for key in ('file', 'sha256', 'complete', 'row_count')):
        raise ValueError('classified rejection artifact aliases differ')
    binding_contract = report.get('binding') or {}
    if not (binding_contract.get('schema') == 'legacy-reconciliation-binding-v3'
            and binding_contract.get('policy') == POLICY
            and binding_contract.get('backend_build_fingerprint') == build
            and binding_contract.get('fixed_input_manifest_sha256') == fixed_input_sha
            and binding_contract.get('clock_projection_manifest_sha256') == sha(clock_manifest['_path'])
            and binding_contract.get('mapping_sha256') == source_manifest.get('raw', {}).get('native_mapping', {}).get('sha256')
            and binding_contract.get('verified_fact_sha256') == report.get('verified_fact_sha256')
            and binding_contract.get('verified_index_sha256') == report.get('verified_index_sha256')):
        raise ValueError('report common v3 binding does not bind this fixed source/build/clock/Store proof')
    for field in ('quote_events_verified', 'quote_events_sha256', 'latest_quotes_verified', 'latest_quotes_sha256'):
        if field not in binding_contract:
            raise ValueError('common v3 binding omits full-generation quote proof: ' + field)
    for field in ('quote_events_sha256', 'latest_quotes_sha256'):
        require_sha(binding_contract[field], field)
    if not all(isinstance(binding_contract.get(field), str) and binding_contract[field].isdigit()
               for field in ('quote_events_verified', 'latest_quotes_verified')):
        raise ValueError('full-generation quote proof counts must be exact decimal strings')
    summaries = {}
    summary_schemas = {'comparison': 'legacy-reconciliation-comparison-v3',
                       'overlay': 'legacy-reconciliation-overlay-v3',
                       'independent_verification': 'legacy-reconciliation-reopen-v3'}
    for key, schema in summary_schemas.items():
        record = report.get(key)
        path = verify_report_artifact(progress, record, json_rows=True)
        value = read(path)
        if value.get('schema') != schema or value.get('complete') is not True:
            raise ValueError(f'{key} summary is incomplete or uses the wrong v3 schema')
        if value.get('binding') != binding_contract:
            raise ValueError(f'{key} summary is bound to different fixed inputs')
        if any(value.get(field) != binding_contract.get(field) for field in binding_contract):
            raise ValueError(f'{key} summary top-level binding fields differ')
        if value.get('empty_raw_seed') is not True or value.get('unresolved_differences') != '0':
            raise ValueError(f'{key} summary does not prove a complete empty-seed result')
        ledger = verify_report_artifact(progress, value.get('ledger'), json_rows=False)
        ledger_count = int(value['ledger']['row_count'])
        if key == 'independent_verification':
            summary_count = int(value.get('selected_bar_rows', '-1')) + int(value.get('selected_quote_rows', '-1'))
        else:
            summary_count = ledger_count
        if int(record.get('row_count', '-1')) != summary_count:
            raise ValueError(f'{key} summary row count differs from its ledger')
        summaries[key] = (value, ledger)
    independent = summaries['independent_verification'][0]
    if (independent.get('expected_field_sha256') != independent.get('actual_field_sha256')
            or independent.get('live_before') != independent.get('live_after')
            or independent.get('proof', {}).get('fact_codec_sha256') != report.get('verified_fact_sha256')
            or independent.get('proof', {}).get('index_codec_sha256') != report.get('verified_index_sha256')
            or independent.get('stage_version', {}).get('committed_capture') is not None):
        raise ValueError('independent reopened full-field verification does not close the inactive target')
    if (independent.get('binding', {}).get('quote_events_verified') != binding_contract['quote_events_verified']
            or independent.get('binding', {}).get('quote_events_sha256') != binding_contract['quote_events_sha256']
            or independent.get('binding', {}).get('latest_quotes_verified') != binding_contract['latest_quotes_verified']
            or independent.get('binding', {}).get('latest_quotes_sha256') != binding_contract['latest_quotes_sha256']):
        raise ValueError('full typed quote event/latest-state evidence differs across summary binding')
    for ledger_key in ('quote_event_ledger', 'latest_quote_ledger', 'selected_input'):
        verify_report_artifact(progress, independent.get(ledger_key), json_rows=False)
    original_proof = independent.get('original_pg_verification') or {}
    if original_proof.get('complete') is not True:
        raise ValueError('complete original PG source verification before overlay is absent')
    if int(report.get('completed_at_ns', '0')) <= int(report.get('started_at_ns', '0')):
        raise ValueError('reconciliation timing is absent or not monotonic')
    report['_rejection_path'] = str(rejection_path)
    return report


def timestamp_ns(value):
    instant = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    delta = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def request(progress, source_manifest, facts, capture, stop, drain, early_tail,
            final_observations, reconciliation, report, tools_manifest, tools_sha):
    progress = Path(progress).resolve(strict=True)
    manifest = read(progress / 'manifest.json')
    fixed = progress / 'fixed-input-manifest.json'
    if not fixed.is_file():
        raise ValueError('immutable original fixed-input-manifest.json missing')
    fixed_sha = sha(fixed)
    stopped, drained = closure_inputs(stop, drain, reconciliation)
    early = read(early_tail)
    tails = [read(path) for path in final_observations]
    raw = manifest['raw']
    expected_identity = (raw['stream'], raw['epoch'], str(raw['incremental_after']['sequence']))
    observations = [early, *tails]
    if len(tails) != 2 or int(tails[0]['observed_at_ns']) >= int(tails[1]['observed_at_ns']):
        raise ValueError('two distinct increasing final tail observations required')
    for tail in observations:
        if (tail.get('stream'), tail.get('epoch'), str(tail.get('last_sequence'))) != expected_identity:
            raise ValueError('early/final raw tail moved or belongs to another stream epoch')
    captured_ns = timestamp_ns(source_manifest['postgres']['captured_at'])
    if not (int(stopped['observed_at_ns']) <= int(early['observed_at_ns']) <= captured_ns
            <= int(read(reconciliation)['started_at_ns'])
            <= int(read(reconciliation)['completed_at_ns'])
            <= int(drained['observed_at_ns'])
            < int(tails[0]['observed_at_ns']) < int(tails[1]['observed_at_ns'])):
        raise ValueError('producer stop, stable early tail, PG snapshot, reconciliation, drain and final tails are out of order')
    if any(int(tail['observed_at_ns']) <= int(drained['observed_at_ns']) for tail in tails):
        raise ValueError('both final stable-tail observations must follow independent drain confirmation')
    if ((tails[0].get('stream'), tails[0].get('epoch'), str(tails[0].get('last_sequence'))) !=
            (tails[1].get('stream'), tails[1].get('epoch'), str(tails[1].get('last_sequence')))
            or str(early.get('last_sequence')) != str(tails[0].get('last_sequence'))):
        raise ValueError('early and both final tail observations must retain one stable stream identity and sequence')
    proofs = [phase.get('proof') for phase in manifest.get('phases', [])
              if phase.get('phase') == 'retained_raw_reconciliation'
              and phase.get('state') == 'verified_composite_inactive'
              and phase.get('activated') is False]
    if not proofs:
        raise ValueError('Rust Store-issued verified composite-inactive proof is missing')
    proof = proofs[-1]
    boundary = {
        'kind': 'legacy_import_authority', 'schema_version': 'tracefang-legacy-authority-v1',
        'production_terminal': True, 'authority_manifest_id': manifest['id'],
        'authority_manifest_sha256': sha(progress / 'manifest.json'),
        'postgres_source_fingerprint': manifest['postgres']['fingerprint'],
        'postgres_snapshot': manifest['postgres']['snapshot'],
        'raw_tail': raw['native_mapping']['last_position'],
        'legacy_stream': raw['stream'], 'legacy_epoch': raw['epoch'],
        'legacy_tail_sequence': str(raw['incremental_after']['sequence']),
        'legacy_mapping_sha256': raw['native_mapping']['sha256'],
        'conflict_policy_version': POLICY,
        'staging_generation': 'legacy-' + manifest['id'],
        'verified_fact_sha256': proof['fact_codec_sha256'],
        'verified_index_sha256': proof['index_codec_sha256'],
        'closure': {
            'stopped_component_ids': stopped['stopped_component_ids'],
            'stop_report_sha256': sha(stop), 'projection_drain_report_sha256': sha(drain),
            'unresolved_frames': '0', 'raw_applied_through_legacy': None,
            'reconciliation_report_sha256': sha(reconciliation),
            'stable_tail_observations': [early, *tails],
        },
        'sealed_tools': {'file': 'terminal-tools-manifest.json', 'sha256': tools_sha,
                         'backend_build_sha256': read(tools_manifest)['backend_build_sha256'],
                         'executor_sha256': sha(Path(read(tools_manifest)['executables']['reconcile-probe']))},
        'fixed_input_manifest_sha256': fixed_sha,
    }
    return {'boundary': boundary, 'stop_report': str(stop), 'drain_report': str(drain),
            'reconciliation_report': str(reconciliation), 'facts_path': str(facts),
            'capture_path': str(capture), 'report_path': str(report)}


def validate_spool_audit(path, fixed_sha, backend_build_sha, terminal_mapping, spool_file):
    path = Path(path).resolve(strict=True)
    proof = read(path)
    manifest = proof.get('manifest') or {}
    full = proof.get('original_complete_decoded_roundtrip') or {}
    raw = terminal_mapping['raw']
    tail = raw['native_mapping']['last_position']
    if not (manifest.get('schema') == 'retained-global-decoded-spool-v2'
            and manifest.get('complete') is True
            and manifest.get('source_manifest_sha256') == fixed_sha
            and manifest.get('build_sha256') == backend_build_sha
            and manifest.get('first', {}).get('sequence') == 1
            and manifest.get('last') == tail
            and int(manifest.get('frames', '-1')) == int(tail['sequence'])
            and manifest.get('original_prefix_complete') is False
            and manifest.get('origin_coverage', {}).get('original_prefix_complete') is False
            and full.get('complete') is True
            and int(full.get('frames', '-1')) == int(tail['sequence'])
            and full.get('expected_sha256') == manifest.get('canonical_decoded_sha256')
            and full.get('actual_sha256') == manifest.get('canonical_decoded_sha256')
            and proof.get('created') is False
            and proof.get('production_modified') is False
            and proof.get('authority_boundary_created') is False):
        raise ValueError('reopened spool audit is incomplete or belongs to another fixed source/build/tail')
    spool_file = Path(spool_file).resolve(strict=True)
    if proof.get('spool_file_sha256') != sha(spool_file):
        raise ValueError('spool bytes differ from the independently reopened spool audit')
    proof['_path'] = str(path)
    proof['_progress_copy_sha256'] = sha(path)
    return proof


def progress_source_phase(progress_manifest, source_directory, facts=None, progress=None):
    phases = [phase for phase in progress_manifest.get('phases', [])
              if phase.get('phase') == 'source_clock_native_staging'
              and phase.get('state') == 'started']
    corrected = [phase for phase in progress_manifest.get('phases', [])
                 if phase.get('phase') == 'corrected_clock_inactive_staging'
                 and phase.get('complete') is True and phase.get('activated') is False]
    if len(phases) != 1 or len(corrected) != 1:
        raise ValueError('progress manifest must contain one started source clock phase and one completed inactive import')
    phase = phases[0]
    completion = corrected[0]
    source_manifest = Path(phase.get('original_source_manifest', '')).resolve(strict=True)
    expected = (Path(source_directory).resolve(strict=True) / 'manifest.json').resolve(strict=True)
    if (source_manifest != expected or sha(source_manifest) != phase.get('original_source_manifest_sha256')
            or completion.get('original_source_manifest_sha256') != phase.get('original_source_manifest_sha256')
            or completion.get('clock_manifest_sha256') != phase.get('clock_manifest_sha256')
            or (facts is not None and Path(completion.get('target', '')).resolve(strict=True) != Path(facts).resolve(strict=True))
            or (progress is not None and Path(completion.get('progress_manifest', '')).resolve(strict=True)
                != Path(progress).resolve(strict=True))
            or not isinstance(completion.get('proof'), dict)
            or completion['proof'].get('complete') is not True):
        raise ValueError('corrected import does not name the exact fresh original PG manifest')
    return source_manifest, phase['original_source_manifest_sha256']


def copy_clock_inputs(progress, source, clock_directory, audit_path, seal, seal_path, seal_sha):
    source = Path(source).resolve(strict=True)
    clock_directory = Path(clock_directory).resolve(strict=True)
    descriptor = clock_directory / 'canonical-bars-clock-v2.manifest.json'
    mapping = read(descriptor)
    audit_path = Path(audit_path).resolve(strict=True)
    records = seal['clock_policy']
    policy_source = Path(records['source']).resolve(strict=True)
    auditor = Path(records['auditor']).resolve(strict=True)
    if sha(policy_source) != records['source_sha256'] or sha(auditor) != records['auditor_sha256']:
        raise ValueError('clock policy source/auditor changed after seal validation')

    fixed = {}
    def copy(source_path, name):
        path = copy_immutable(source_path, Path(progress) / name, sha(source_path))
        fixed[name] = {'file': name, 'sha256': sha(path)}
        return path

    copy(descriptor, 'terminal-clock-manifest.json')
    copy(audit_path, 'terminal-clock-audit.json')
    copy(seal_path, 'terminal-tools-manifest.json')
    copy(policy_source, 'terminal-clock-policy-source.rs')
    copy(auditor, 'terminal-clock-auditor.py')
    witnesses = []
    for item in records['witnesses']:
        witness = Path(item['file']).resolve(strict=True)
        if sha(witness) != item['sha256']:
            raise ValueError('sealed clock witness changed: ' + str(witness))
        if witness.name in fixed or witness.name in witnesses:
            raise ValueError('clock witness basename collides with another progress artifact')
        copy(witness, witness.name)
        witnesses.append(witness.name)
    # The independent auditor must identify its exact source bytes, and the
    # source projection must identify the exact source/witness evidence.
    if (stable_read(audit_path) and read(audit_path).get('oracle_source_sha256') != records['auditor_sha256']):
        raise ValueError('independent root clock audit does not bind the sealed auditor source')
    if (mapping.get('policy_source_sha256') != records['source_sha256']
            or [(Path(item['file']).name, item['sha256']) for item in mapping.get('policy_evidence', [])]
            != [(Path(item['file']).name, item['sha256']) for item in records['witnesses']]):
        raise ValueError('clock projection worker/witness inputs differ from the final seal')
    fixed['policy_evidence'] = [{'file': name, 'sha256': sha(Path(progress) / name)} for name in witnesses]
    fixed['policy_source'] = fixed['terminal-clock-policy-source.rs']
    fixed['auditor_source'] = fixed['terminal-clock-auditor.py']
    mapping['_path'] = str(Path(progress) / 'terminal-clock-manifest.json')
    fixed['mapping_manifest'] = fixed['terminal-clock-manifest.json']
    fixed['independent_audit'] = fixed['terminal-clock-audit.json']
    fixed['_mapping'] = mapping
    fixed['_audit_sha256'] = sha(Path(progress) / 'terminal-clock-audit.json')
    fixed['_seal_copy_sha256'] = sha(Path(progress) / 'terminal-tools-manifest.json')
    return fixed


def required_seal_path(seal, name, value):
    expected = Path(seal['executables'][name]).resolve(strict=True)
    actual = Path(value).resolve(strict=True)
    if actual != expected:
        raise ValueError(f'--{name} must select the sealed copied Release helper')
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare-terminal-inputs', 'reconcile', 'verify', 'activate'))
    parser.add_argument('--probe', type=Path, required=True)
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--base-source', type=Path, required=True)
    parser.add_argument('--base-facts', type=Path, required=True,
                        help='original base facts path; it may be absent only with its named verified archive receipt')
    parser.add_argument('--base-capture', type=Path,
                        help='quiescent original native capture matching the base raw mapping; cloned before delta import')
    parser.add_argument('--archived-input-receipt', type=Path)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--facts', type=Path, required=True)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--stop-report', type=Path, required=True)
    parser.add_argument('--drain-report', type=Path)
    parser.add_argument('--reconciliation-report', type=Path)
    parser.add_argument('--reconcile-probe', type=Path)
    parser.add_argument('--clock-probe', type=Path)
    parser.add_argument('--clock-policy-source', type=Path)
    parser.add_argument('--clock-policy-evidence', type=Path, action='append', default=[])
    parser.add_argument('--clock-auditor', type=Path)
    parser.add_argument('--clock-directory', type=Path)
    parser.add_argument('--progress-directory', type=Path)
    parser.add_argument('--spool', type=Path)
    parser.add_argument('--tools-manifest', type=Path, required=True)
    parser.add_argument('--resume-export-evidence', type=Path,
                        help='preserved journal with only the inputs gate, early tail and terminal export completed')
    parser.add_argument('--resume-tools-manifest', type=Path,
                        help='exact preserved tool seal bound to the exported-source journal')
    parser.add_argument('--facts-cap-gib', type=int, default=16)
    parser.add_argument('--spool-cap-gib', type=int, default=NATIVE_SPOOL_CAP_GIB,
                        choices=(NATIVE_SPOOL_CAP_GIB,),
                        help='fixed native spool physical file cap in GiB')
    args = parser.parse_args()
    if bool(args.resume_export_evidence) != bool(args.resume_tools_manifest):
        raise ValueError('export resume requires both prior evidence and its tool seal')
    if args.resume_export_evidence and args.phase != 'prepare-terminal-inputs':
        raise ValueError('export resume is only valid during input preparation')
    for key in ('backend', 'base_source', 'stop_report'):
        setattr(args, key, getattr(args, key).resolve(strict=True))
    for key in ('probe', 'base_facts', 'capture', 'source', 'facts', 'evidence'):
        setattr(args, key, getattr(args, key).resolve())
    if args.archived_input_receipt:
        args.archived_input_receipt = args.archived_input_receipt.resolve(strict=True)
    args.drain_report = (args.drain_report or args.evidence/'drain-confirmed.json').resolve()
    args.clock_directory = (args.clock_directory or args.facts.parent/'clock-source').resolve()
    args.progress_directory = (args.progress_directory or args.facts.parent/'authority-sources').resolve()
    args.reconciliation_report = (args.reconciliation_report or args.progress_directory/'terminal-reconciliation.json').resolve()
    args.spool = (args.spool or args.evidence/'terminal-global-spool.ndjson').resolve()
    if args.reconciliation_report.parent != args.progress_directory:
        raise ValueError('the terminal reconciliation report must be stored beside its basename artifacts in progress')
    if not 1 <= args.facts_cap_gib <= 16:
        raise ValueError('facts cap must be explicit positive GiB no greater than16')
    args.tools_manifest = args.tools_manifest.resolve(strict=True)
    seal_sha = sha(args.tools_manifest)
    seal, sealed, roles = verify_seal(args.tools_manifest, seal_sha)
    args.probe = required_seal_path(seal, 'probe', args.probe)
    args.reconcile_probe = required_seal_path(seal, 'reconcile-probe',
        args.reconcile_probe or seal['executables']['reconcile-probe'])
    args.clock_probe = required_seal_path(seal, 'clock-probe',
        args.clock_probe or seal['executables']['clock-probe'])
    args.clock_policy_source = Path(args.clock_policy_source or seal['clock_policy']['source']).resolve(strict=True)
    args.clock_auditor = Path(args.clock_auditor or seal['clock_policy']['auditor']).resolve(strict=True)
    requested_witnesses = list(args.clock_policy_evidence)
    expected_source = Path(seal['clock_policy']['source']).resolve(strict=True)
    expected_auditor = Path(seal['clock_policy']['auditor']).resolve(strict=True)
    if args.clock_policy_source != expected_source or args.clock_auditor != expected_auditor:
        raise ValueError('clock policy source/auditor must be the exact sealed files')
    args.clock_policy_evidence = [Path(item['file']).resolve(strict=True)
                                  for item in seal['clock_policy']['witnesses']]
    # The parser's default is empty; if the user supplied any witnesses, they
    # must exactly match the sealed ordered evidence list.
    if requested_witnesses and [p.resolve(strict=True) for p in requested_witnesses] != args.clock_policy_evidence:
        raise ValueError('clock policy evidence must exactly match the sealed witness list')
    scripts = Path(__file__).resolve().parent
    preflight_script = scripts/'migration-space-preflight.py'
    preflight_script = preflight_script.resolve(strict=True)
    if preflight_script not in sealed:
        raise ValueError('migration-space-preflight.py is not included in the complete terminal seal')
    preflight_ns = runpy.run_path(str(preflight_script))
    archive_receipt = load_archive_receipt(args.archived_input_receipt, preflight_ns)
    base_manifest_path = args.base_source/'manifest.json'
    if args.phase == 'prepare-terminal-inputs':
        # The old export manifest, raw archive, mapping and configs remain
        # byte-readable; named receipts can cover only retired facts/tables/canonical.
        _base_manifest, base_pins = verify_source_snapshot(args.base_source, archive_receipt, True)
        if not base_manifest_path.is_file():
            raise ValueError('base source manifest is required for terminal incremental export')
    if args.probe.resolve(strict=True) not in sealed:
        raise ValueError('selected terminal probe is not sealed')
    args.evidence.mkdir(parents=True, exist_ok=True)
    journal = args.evidence / 'handoff-journal.json'
    state = read(journal) if journal.exists() else {'schema': 'terminal-handoff-journal-v1', 'events': [],
                'old_services_changed_by_this_tool': False, 'providers_started_by_this_tool': False,
                'facts_path': str(args.facts), 'source_path': str(args.source), 'capture_path': str(args.capture),
                'progress_path': str(args.progress_directory), 'clock_path': str(args.clock_directory),
                'tools_manifest_sha256': seal_sha,
                'archived_input_receipt': ({'file': str(args.archived_input_receipt),
                    'sha256': sha(args.archived_input_receipt)} if args.archived_input_receipt else None)}
    if any(state.get(key) != str(getattr(args, argument)) for key, argument in
           [('facts_path', 'facts'), ('source_path', 'source'), ('capture_path', 'capture'),
            ('progress_path', 'progress_directory'), ('clock_path', 'clock_directory')]):
        raise ValueError('existing journal belongs to another target')
    if state['tools_manifest_sha256'] != seal_sha:
        raise ValueError('terminal journal is bound to different frozen migration tools')
    expected_receipt = ({'file': str(args.archived_input_receipt), 'sha256': sha(args.archived_input_receipt)}
                        if args.archived_input_receipt else None)
    if state.get('archived_input_receipt') != expected_receipt:
        raise ValueError('terminal journal is bound to a different named archive receipt')
    def event(phase, outcome, **details):
        state['events'].append({'at': datetime.now(timezone.utc).isoformat(), 'phase': phase,
                                'state': outcome, **details})
        save(journal, state, replace=journal.exists())
    def run(label, command, input_paths=()):
        # Re-read the entire seal before every executable phase. That includes
        # the actual Python runtime, every script, every Release helper, policy,
        # witnesses, auditor and the tool seal itself.
        fresh_seal, fresh_sealed, _fresh_roles = verify_seal(args.tools_manifest, seal_sha)
        verify_execution(command, fresh_sealed)
        if fresh_seal['backend_build_sha256'] != seal['backend_build_sha256']:
            raise ValueError('sealed whole-backend fingerprint changed before execution')
        pins = {Path(path).resolve(strict=True): expected for path, expected in input_paths}
        verify_pins(pins)
        spool_build = label == 'global-spool-build'
        limit_details = ({'file_cap_bytes': NATIVE_SPOOL_CAP_GIB * 1024**3}
                         if spool_build else {})
        event(label, 'started', **limit_details)
        log = args.evidence / (f'{len(state["events"]):04d}-' + label + '.log')
        with os.fdopen(os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as output:
            completed = subprocess.run([str(part) for part in command], cwd=args.backend,
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                       **({'preexec_fn': spool_child_limit} if spool_build else {}))
            output.flush(); os.fsync(output.fileno())
        event(label, 'complete' if completed.returncode == 0 else 'failed',
              exit_code=completed.returncode, log=str(log))
        if completed.returncode:
            raise RuntimeError(f'{label} failed; preserved source/receipts/log: {log}')
    def preflight(label, source_dir, facts_path, phase, facts_kind, allow_archived, extra=()):
        report_path = args.evidence/(label + '.json')
        if report_path.exists() or report_path.is_symlink():
            raise FileExistsError(f'preflight receipt already exists; preserve it and select a fresh evidence directory: {report_path}')
        command = space_preflight_command(args, preflight_script, source_dir, facts_path,
                                          phase, facts_kind, report_path)
        if allow_archived:
            command.append('--allow-archived-base-source')
        if args.archived_input_receipt:
            command.extend(['--archived-input-receipt', args.archived_input_receipt])
        pins = list(extra)
        if args.archived_input_receipt:
            pins.append((args.archived_input_receipt, sha(args.archived_input_receipt)))
        if Path(facts_path).is_file() and not Path(facts_path).is_symlink():
            pins.append((Path(facts_path), sha(facts_path)))
        run(label, command, input_paths=pins)
        result = read(report_path)
        if (result.get('schema') != 'migration-space-preflight-v2'
                or result.get('space_plan', {}).get('archive_credit_bytes') != '0'
                or result.get('space_plan', {}).get('phase_admitted') is not True):
            raise ValueError(f'{label} fresh phase-space gate did not admit this phase')
        return report_path

    def write_fixed_input_manifest(progress):
        source_path = Path(progress)/'manifest.json'
        frozen = Path(progress)/'fixed-input-manifest.json'
        copy_immutable(source_path, frozen, sha(source_path))
        value = read(frozen)
        if (value.get('id') != read(source_path).get('id')
                or sha(frozen) != sha(source_path)):
            raise ValueError('fixed original PG/source manifest copy changed during freeze')
        return frozen

    def pin_source(directory, allow_archived=False, require_mapping=True):
        return verify_source_snapshot(directory, archive_receipt, allow_archived, require_mapping)

    try:
        if args.phase == 'prepare-terminal-inputs':
            # Check maximum allowed coexistence BEFORE a fleet stop is allowed.
            # This terminal invocation checks it again; root uses standalone preflight before stopping.
            stopped = stop_input(args.stop_report)
            if args.base_capture is None:
                raise ValueError('terminal delta import requires its exact original native base capture')
            fresh = (args.facts, args.clock_directory, args.progress_directory,
                     args.capture, args.spool, args.reconciliation_report)
            if any(path.exists() for path in (*fresh, *(() if args.resume_export_evidence else (args.source,)))):
                raise ValueError('fresh terminal source/facts required; preserve failed targets, resume explicitly through probe')
            outputs = [journal, args.evidence/'space-preflight-inputs.json',
                       args.evidence/'tail-before.json', args.evidence/'space-preflight-facts.json',
                       args.evidence/'independent-terminal-clock-audit.json',
                       args.evidence/'terminal-corrected-import.json',
                       args.evidence/'terminal-clock-series-state-oracle.json',
                       args.evidence/'terminal-clock-state-repair.json',
                       args.evidence/'terminal-all-bars-source-verify.json',
                       args.evidence/'space-preflight-spool.json',
                       args.evidence/'terminal-spool-build.json', args.evidence/'terminal-spool-audit.json']
            if any(path.exists() or path.is_symlink() for path in outputs):
                raise FileExistsError('prepare phase output already exists; preserve receipts and select a fresh evidence directory')
            resumed = verify_export_resume(args, seal) if args.resume_export_evidence else None
            preflight('space-preflight-inputs', args.base_source, args.base_facts,
                      'final_inputs', 'base', True,
                      extra=[(path, digest) for path, digest in base_pins.items()])
            if resumed:
                tail_path, checkpoint = resumed
                copy_immutable(tail_path, args.evidence/'tail-before.json', sha(tail_path))
                event('terminal-export', 'reused_verified_export_checkpoint', **checkpoint)
            else:
                run('tail-before', [args.probe, 'tail-observe', args.evidence, args.evidence/'tail-before.json'],
                    input_paths=[(base_manifest_path, sha(base_manifest_path))])
            if int(read(args.evidence/'tail-before.json')['observed_at_ns']) < int(stopped['observed_at_ns']):
                raise ValueError('early tail must be a real observation after producer stop')
            if not resumed:
                run('terminal-export', [args.probe, 'terminal-export', args.source, base_manifest_path],
                    input_paths=[(path, digest) for path, digest in base_pins.items()])
            source_manifest, source_pins = pin_source(args.source, require_mapping=False)
            base_raw = read(base_manifest_path)['raw']
            if source_manifest['raw']['incremental_base']['source_manifest_sha256'] != sha(base_manifest_path):
                raise ValueError('terminal raw increment must retain the selected base source')
            snapshot = clone_capture_snapshot(args.base_capture, args.capture)
            event('base-capture-snapshot', 'complete', **snapshot)
            base_validation = args.evidence/'base-prefix-validation'
            base_validation.mkdir()
            copy_immutable(base_manifest_path, base_validation/'manifest.json', sha(base_manifest_path))
            base_map = args.base_source/base_raw['native_mapping']['file']
            copy_immutable(base_map, base_validation/base_map.name, base_raw['native_mapping']['sha256'])
            run('base-raw-verify', [args.probe, 'raw-verify', base_validation, args.capture],
                input_paths=[(base_manifest_path, sha(base_manifest_path)),
                             (base_map, base_raw['native_mapping']['sha256']),
                             (args.capture, snapshot['sha256'])])
            base_verified = read(base_validation/'manifest.json')['phases'][-1]
            last = base_raw['native_mapping']['last_position']
            if (base_verified.get('state') != 'complete_reopened_every_envelope'
                    or base_verified.get('verified_last_position') != last
                    or base_verified.get('bounds', {}).get('last_sequence') != last['sequence']
                    or base_verified.get('bounds', {}).get('epoch') != last['epoch']):
                raise ValueError('base capture does not end at its independently verified native mapping')
            with tempfile.TemporaryDirectory(prefix='canonical-terminal-', dir=args.evidence) as derived:
                run('canonical-select', [sys.executable, scripts/'prepare-legacy-canonical-bars.py', args.source, derived],
                    input_paths=[(path, digest) for path, digest in source_pins.items()])
            source_manifest, source_pins = pin_source(args.source, require_mapping=False)
            run('raw-import', [args.probe, 'raw-import', args.source, args.capture],
                input_paths=[(path, digest) for path, digest in source_pins.items()])
            source_manifest, source_pins = pin_source(args.source)
            run('raw-verify', [args.probe, 'raw-verify', args.source, args.capture],
                input_paths=[(path, digest) for path, digest in source_pins.items()])
            source_manifest, source_pins = pin_source(args.source)
            # Mapping and the independent audit both use THIS final PG snapshot.
            # A rehearsal audit cannot certify a new source file or revision.
            clock_cache = args.evidence/'clock-planner-cache'
            run('clock-project', [sys.executable, scripts/'prepare-legacy-clock-canonical.py',
                args.source, args.clock_directory, clock_cache, '--policy-probe', args.clock_probe,
                '--policy-source', args.clock_policy_source,
                *[part for path in args.clock_policy_evidence for part in ('--policy-evidence', path)]],
                input_paths=[*[(path, digest) for path, digest in source_pins.items()],
                             (args.clock_probe, sha(args.clock_probe)),
                             (args.clock_policy_source, sha(args.clock_policy_source)),
                             *[(path, sha(path)) for path in args.clock_policy_evidence]])
            audit = args.evidence/'independent-terminal-clock-audit.json'
            run('independent-clock-audit', [sys.executable, args.clock_auditor, args.source, args.clock_directory, audit],
                input_paths=[(path, digest) for path, digest in source_pins.items()])
            _manifest, _original, mapping, _audit, mapping_path, corrected_path = validate_clock_inputs(
                args.source, args.clock_directory, audit)
            clock_pins = {mapping_path: sha(mapping_path), corrected_path: sha(corrected_path), audit: sha(audit)}
            for item in mapping.get('artifacts', {}).values():
                path = args.clock_directory/item['file']
                if sha(path) != item['sha256']:
                    raise ValueError('clock mapping evidence changed after independent audit')
                clock_pins[path] = item['sha256']
            # Only this regenerable planner scratch is released after its full
            # output was independently checked. Original sources remain intact.
            shutil.rmtree(clock_cache)
            preflight('space-preflight-facts', args.source, args.base_facts,
                      'fresh_facts', 'base', False,
                      extra=[*[(path, digest) for path, digest in source_pins.items()],
                             *[(path, digest) for path, digest in clock_pins.items()]])
            run('facts-clock-import', [args.probe, 'facts-clock-import', args.source, args.facts,
                args.clock_directory, args.progress_directory, audit,
                args.evidence/'terminal-corrected-import.json', args.facts_cap_gib*1024**3],
                input_paths=[*[(path, digest) for path, digest in source_pins.items()],
                             *[(path, digest) for path, digest in clock_pins.items()]])
            state_oracle = args.evidence/'terminal-clock-series-state-oracle.json'
            run('prepare-clock-series-state', [sys.executable, scripts/'prepare-clock-series-state.py',
                args.source, args.clock_directory, state_oracle],
                input_paths=[*[(path, digest) for path, digest in source_pins.items()],
                             *[(path, digest) for path, digest in clock_pins.items()]])
            state_repair = args.evidence/'terminal-clock-state-repair.json'
            run('clock-state-repair', [args.probe, 'clock-state-repair', args.source, args.facts,
                args.clock_directory, args.progress_directory, audit, state_oracle, state_repair],
                input_paths=[*[(path, digest) for path, digest in source_pins.items()],
                             *[(path, digest) for path, digest in clock_pins.items()],
                             (state_oracle, sha(state_oracle)),
                             (args.facts, sha(args.facts))])
            repaired = read(state_repair)
            if (repaired.get('phase') != 'runtime_clock_series_state_repair'
                    or repaired.get('state') != 'complete_inactive'
                    or repaired.get('activated') is not False
                    or repaired.get('receipt', {}).get('complete') is not True):
                raise ValueError('typed inactive clock series-state repair did not independently complete')
            run('clock-bars-verify', [args.probe, 'clock-bars-verify', args.source, args.facts,
                args.clock_directory, args.evidence/'terminal-all-bars-source-verify.json'],
                input_paths=[*[(path, digest) for path, digest in source_pins.items()],
                             *[(path, digest) for path, digest in clock_pins.items()],
                             (args.facts, sha(args.facts))])
            run('quotes-verify', [args.probe, 'quotes-verify', args.progress_directory, args.facts],
                input_paths=[*[(path, digest) for path, digest in source_pins.items()],
                             *[(path, digest) for path, digest in clock_pins.items()],
                             (args.facts, sha(args.facts))])
            frozen = write_fixed_input_manifest(args.progress_directory)
            progress_manifest = read(args.progress_directory/'manifest.json')
            original_manifest, original_source_sha = progress_source_phase(
                progress_manifest, args.source, args.facts, args.progress_directory)
            if sha(original_manifest) != original_source_sha:
                raise ValueError('original fresh PG source manifest changed before fixed-input freeze')
            preflight('space-preflight-spool', args.source, args.facts,
                      'retained_reconciliation', 'fresh', False,
                      extra=[*[(path, digest) for path, digest in source_pins.items()],
                             *[(path, digest) for path, digest in clock_pins.items()],
                             (frozen, sha(frozen)), (args.facts, sha(args.facts)),
                             (args.capture, sha(args.capture))])
            early = read(args.evidence/'tail-before.json')
            native_through = native_spool_through(read(frozen), early)
            spool_build_report = args.evidence/'terminal-spool-build.json'
            run('global-spool-build', [seal['executables']['spool-probe'], 'build', args.capture,
                frozen, str(native_through), args.spool, spool_build_report],
                input_paths=[(frozen, sha(frozen)), (args.capture, sha(args.capture))])
            spool_audit_report = args.evidence/'terminal-spool-audit.json'
            run('global-spool-audit', [seal['executables']['spool-probe'], 'audit', args.capture,
                frozen, str(native_through), args.spool, spool_audit_report],
                input_paths=[(frozen, sha(frozen)), (args.capture, sha(args.capture)),
                             (args.spool, sha(args.spool))])
            validate_spool_audit(spool_audit_report, sha(frozen), seal['backend_build_sha256'],
                                 read(args.progress_directory/'manifest.json'), args.spool)
            copy_immutable(spool_audit_report, args.progress_directory/'terminal-spool-audit.json',
                           sha(spool_audit_report))
            progress_manifest = read(args.progress_directory/'manifest.json')
            original_manifest, original_source_sha = progress_source_phase(
                progress_manifest, args.source, args.facts, args.progress_directory)
            clock_copies = copy_clock_inputs(args.progress_directory, args.source,
                args.clock_directory, audit, seal, args.tools_manifest, seal_sha)
            event('prepare-terminal-inputs', 'verified_inputs_waiting_for_independent_reconciliation',
                  fixed_input_manifest_sha256=sha(frozen), original_source_manifest_sha256=original_source_sha,
                  global_spool_sha256=sha(args.spool), spool_audit_sha256=sha(args.progress_directory/'terminal-spool-audit.json'),
                  activation=False,
                  raw_applied_through_legacy=None, clean_drain_proven=False)
        elif args.phase == 'reconcile':
            outputs = [args.evidence/'space-preflight-retained.json', args.drain_report,
                       args.evidence/'tail-final-first.json', args.evidence/'tail-final-second.json',
                       args.evidence/'authority-request.json', args.evidence/'authority-verified.json',
                       args.reconciliation_report]
            if any(path.exists() or path.is_symlink() for path in outputs):
                raise FileExistsError('reconcile phase output already exists; preserve receipts and select fresh report paths')
            stopped = stop_input(args.stop_report)
            source_manifest, source_pins = pin_source(args.source)
            progress_manifest = read(args.progress_directory/'manifest.json')
            original_manifest, original_source_sha = progress_source_phase(
                progress_manifest, args.source, args.facts, args.progress_directory)
            fixed = args.progress_directory/'fixed-input-manifest.json'
            fixed_sha = sha(fixed)
            if read(fixed).get('id') != progress_manifest.get('id'):
                raise ValueError('fixed input manifest identifies another PG export')
            seal_copy_sha = sha(args.progress_directory/'terminal-tools-manifest.json')
            if seal_copy_sha != seal_sha:
                raise ValueError('progress seal copy differs from the selected immutable tool seal')
            clock_copies = copy_clock_inputs(args.progress_directory, args.source,
                args.clock_directory, args.evidence/'independent-terminal-clock-audit.json',
                seal, args.tools_manifest, seal_sha) if not (args.progress_directory/'terminal-clock-manifest.json').exists() else None
            # Every run checks the copied clock/audit/seal bytes, fresh mapping,
            # raw lineage source, fixed manifest, spool and original tail.
            clock_manifest_copy = read(args.progress_directory/'terminal-clock-manifest.json')
            audit_copy_path = args.progress_directory/'terminal-clock-audit.json'
            clock_audit_copy = read(audit_copy_path)
            _manifest, _original, mapping, _audit, mapping_path, corrected_path = validate_clock_inputs(
                args.source, args.clock_directory, args.evidence/'independent-terminal-clock-audit.json')
            if (sha(mapping_path) != sha(args.progress_directory/'terminal-clock-manifest.json')
                    or sha(audit_copy_path) != sha(args.evidence/'independent-terminal-clock-audit.json')):
                raise ValueError('copied clock projection/audit is not the exact fresh source audit')
            copied_spool_audit = args.progress_directory/'terminal-spool-audit.json'
            spool_audit = validate_spool_audit(copied_spool_audit, fixed_sha,
                seal['backend_build_sha256'], progress_manifest, args.spool)
            validate_clock_inputs(args.source, args.clock_directory,
                args.evidence/'independent-terminal-clock-audit.json')
            preflight('space-preflight-retained', args.source, args.facts,
                'retained_reconciliation', 'fresh', False,
                extra=[*[(path, digest) for path, digest in source_pins.items()],
                       (fixed, fixed_sha), (args.facts, sha(args.facts)),
                       (args.capture, sha(args.capture)), (args.spool, sha(args.spool)),
                       (args.progress_directory/'terminal-tools-manifest.json', seal_sha),
                       (args.progress_directory/'terminal-clock-manifest.json', sha(args.progress_directory/'terminal-clock-manifest.json')),
                       (audit_copy_path, sha(audit_copy_path)),
                       (copied_spool_audit, sha(copied_spool_audit))])
            early_path = args.evidence/'tail-before.json'
            early = read(early_path)
            expected_executor_sha = sha(args.reconcile_probe)
            run('independent-reconcile-v3', [args.reconcile_probe, 'reconcile-v3',
                args.progress_directory, args.facts, args.capture, args.clock_directory, args.spool,
                audit_copy_path, early_path, args.reconciliation_report,
                '--production-terminal', '--tools-manifest', args.progress_directory/'terminal-tools-manifest.json'],
                input_paths=[*[(path, digest) for path, digest in source_pins.items()],
                       (fixed, fixed_sha), (args.facts, sha(args.facts)),
                       (args.capture, sha(args.capture)), (args.spool, sha(args.spool)),
                       (early_path, sha(early_path)),
                       *[(path, sha(path)) for path in (args.progress_directory/'terminal-tools-manifest.json',
                           args.progress_directory/'terminal-clock-manifest.json', audit_copy_path, copied_spool_audit,
                           args.progress_directory/'terminal-clock-policy-source.rs',
                           args.progress_directory/'terminal-clock-auditor.py')]])
            reconciliation = validate_reconciliation(args.reconciliation_report, source_manifest,
                fixed_sha, {'_path': str(args.progress_directory/'terminal-clock-manifest.json'),
                            'original_canonical_file_sha256': mapping['original_canonical_file_sha256'],
                            'sha256': mapping['sha256']},
                original_source_sha, sha(audit_copy_path), seal, expected_executor_sha,
                spool_audit, args.progress_directory, clock_copies or {
                    'manifest': {'file':'terminal-clock-manifest.json','sha256':sha(args.progress_directory/'terminal-clock-manifest.json')},
                    'independent_audit': {'file':'terminal-clock-audit.json','sha256':sha(audit_copy_path)},
                    'policy_source': {'file':'terminal-clock-policy-source.rs','sha256':sha(args.progress_directory/'terminal-clock-policy-source.rs')},
                    'auditor_source': {'file':'terminal-clock-auditor.py','sha256':sha(args.progress_directory/'terminal-clock-auditor.py')},
                    'policy_evidence': [{'file':Path(item['file']).name,'sha256':item['sha256']}
                                        for item in seal['clock_policy']['witnesses']]})
            # Independent reconciliation is the only drain proof. It explicitly
            # leaves the legacy writer cursor unclaimed and the authority inactive.
            save(args.drain_report, {'schema': 'legacy-drain-evidence-v1', 'production_terminal': True,
                'stream': reconciliation['stream'], 'epoch': reconciliation['epoch'],
                'method': 'independent_retained_raw_reconciliation', 'raw_applied_through_legacy': None,
                'unresolved_frames': '0', 'observed_at_ns': str(time.time_ns()),
                'reconciliation_report_sha256': sha(args.reconciliation_report)})
            run('tail-final-first', [args.probe, 'tail-observe', args.evidence, args.evidence/'tail-final-first.json'],
                input_paths=[(args.capture, sha(args.capture)), (fixed, fixed_sha)])
            # Require two actual observations with an interval, never two copies.
            time.sleep(5)
            run('tail-final-second', [args.probe, 'tail-observe', args.evidence, args.evidence/'tail-final-second.json'],
                input_paths=[(args.capture, sha(args.capture)), (fixed, fixed_sha)])
            spec = request(args.progress_directory, source_manifest, args.facts, args.capture,
                    args.stop_report, args.drain_report,
                    args.evidence/'tail-before.json',
                    [args.evidence/'tail-final-first.json', args.evidence/'tail-final-second.json'],
                    args.reconciliation_report, args.evidence/'authority-verified.json',
                    args.progress_directory/'terminal-tools-manifest.json', seal_sha)
            save(args.evidence/'authority-request.json', spec)
            run('boundary-verify', [args.probe, 'boundary-verify', args.progress_directory, args.evidence/'authority-request.json'],
                input_paths=[(args.progress_directory/'manifest.json', sha(args.progress_directory/'manifest.json')),
                             (fixed, fixed_sha), (args.reconciliation_report, sha(args.reconciliation_report)),
                             (args.evidence/'authority-request.json', sha(args.evidence/'authority-request.json'))])
            event('reconcile', 'verified_composite_inactive', activation=False)
        elif args.phase == 'verify':
            run('boundary-reverify', [args.probe, 'boundary-verify', args.progress_directory, args.evidence/'authority-request.json'],
                input_paths=[(args.progress_directory/'manifest.json', sha(args.progress_directory/'manifest.json')),
                             (args.progress_directory/'fixed-input-manifest.json', sha(args.progress_directory/'fixed-input-manifest.json')),
                             (args.reconciliation_report, sha(args.reconciliation_report)),
                             (args.evidence/'authority-request.json', sha(args.evidence/'authority-request.json'))])
        else:
            # Separate explicit fleet command; no provider starts or PG/raw projection cursor fabrication.
            spec = read(args.evidence/'authority-request.json')
            spec['report_path'] = str(args.evidence/'authority-activated.json')
            save(args.evidence/'activation-request.json', spec)
            run('boundary-activate', [args.probe, 'boundary-activate', args.progress_directory, args.evidence/'activation-request.json'],
                input_paths=[(args.progress_directory/'manifest.json', sha(args.progress_directory/'manifest.json')),
                             (args.progress_directory/'fixed-input-manifest.json', sha(args.progress_directory/'fixed-input-manifest.json')),
                             (args.reconciliation_report, sha(args.reconciliation_report)),
                             (args.evidence/'activation-request.json', sha(args.evidence/'activation-request.json'))])
            event('activate', 'terminal_authority_installed', providers_started=False)
    except Exception as error:
        event(args.phase, 'failed_preserved', error=str(error),
              recovery='Do not start native providers. Preserve all source/capture/facts. Before activation the old collector may be explicitly resumed by root; then discard terminal authority assumptions and take a new quiet snapshot. After activation root must first inspect actual native committed cursor; do not run both collectors or reverse an applied native prefix.')
        raise
    print(json.dumps({'phase': args.phase, 'journal': str(journal), 'providers_started': False}))

if __name__ == '__main__':
    main()
