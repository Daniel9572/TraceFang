#!/usr/bin/env python3
"""Copy frozen migration executors out of Cargo target and seal their inputs.

This creates a new seal only. It never overwrites a helper, seal or source file.
The terminal handoff wrapper verifies every file in this manifest before each
phase and verifies the current Python runtime against the recorded authority.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


SCHEMA = 'tracefang-migration-tools-v1'
POLICY = 'legacy-bars-fixed-authority+clock-projection+retained-raw-overlay-v2'
REQUIRED_SCRIPTS = {
    'migration-terminal-handoff.py', 'migration-space-preflight.py',
    'legacy-stop-evidence.py', 'prepare-legacy-canonical-bars.py',
    'prepare-legacy-clock-canonical.py', 'prepare-clock-series-state.py',
    'seal-migration-tools.py',
}
NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def sha(path):
    path = Path(path)
    before = os.stat(path, follow_symlinks=False)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'sealed input must be a regular non-symlink file: {path}')
    digest = hashlib.sha256()
    count = 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if identity(opened) != identity(before):
            raise ValueError(f'sealed input changed before hashing: {path}')
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            count += len(block)
        after_fd = os.fstat(stream.fileno())
    after = os.stat(path, follow_symlinks=False)
    if count != before.st_size or identity(after_fd) != identity(before) or identity(after) != identity(before):
        raise ValueError(f'sealed input was short-hashed or changed: {path}')
    return digest.hexdigest()


def path_file(value, label, allow_symlink=False):
    raw = Path(value).expanduser()
    if raw.is_symlink() and not allow_symlink:
        raise ValueError(f'{label} cannot be a symlink: {raw}')
    path = raw.resolve(strict=True)
    if not path.is_file():
        raise ValueError(f'{label} must be a regular file: {path}')
    return path


def in_mutable_target(path, backend_root, target_dir=None):
    roots = [(backend_root / 'target').resolve(),
             (Path.home() / 'Library' / 'Caches' / 'TraceFang' / 'rust-target').resolve()]
    if target_dir:
        roots.append(Path(target_dir).resolve())
    if os.environ.get('CARGO_TARGET_DIR'):
        roots.append(Path(os.environ['CARGO_TARGET_DIR']).expanduser().resolve())
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            pass
    return False


def copy_frozen(source, destination, expected_sha256=None):
    """Create an exclusive executable copy and return its immutable digest."""
    before = sha(source)
    if expected_sha256 is not None and before != expected_sha256:
        raise ValueError(f'executor source does not match its fixed authority SHA: {source}')
    before_stat = os.stat(source, follow_symlinks=False)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o500)
    digest = hashlib.sha256()
    copied_bytes = 0
    try:
        with source.open('rb') as incoming, os.fdopen(descriptor, 'wb') as outgoing:
            if identity(os.fstat(incoming.fileno())) != identity(before_stat):
                raise ValueError(f'executor source changed before copy: {source}')
            for block in iter(lambda: incoming.read(1024 * 1024), b''):
                outgoing.write(block)
                digest.update(block)
                copied_bytes += len(block)
            after_fd = os.fstat(incoming.fileno())
            outgoing.flush()
            os.fsync(outgoing.fileno())
        mode = stat.S_IMODE(source.stat().st_mode) & 0o555
        os.chmod(destination, mode or 0o500)
    except BaseException:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise
    after = sha(source)
    copied = digest.hexdigest()
    after_stat = os.stat(source, follow_symlinks=False)
    destination_stat = os.stat(destination, follow_symlinks=False)
    if (before != after or before != copied or copied_bytes != before_stat.st_size
            or (expected_sha256 is not None and after != expected_sha256)
            or identity(after_fd) != identity(before_stat) or identity(after_stat) != identity(before_stat)
            or destination_stat.st_size != before_stat.st_size):
        destination.unlink(missing_ok=True)
        raise ValueError(f'executor changed while copying: {source}')
    return copied


def parse_named(values, label):
    result = {}
    for item in values:
        if '=' not in item:
            raise ValueError(f'{label} must use NAME=PATH')
        name, raw_path = item.split('=', 1)
        if not NAME.fullmatch(name) or name in result:
            raise ValueError(f'invalid or duplicate {label} name: {name}')
        result[name] = raw_path
    return result


def parse_tool_hashes(values):
    result = {}
    for item in values:
        if '=' not in item:
            raise ValueError('tool SHA must use NAME=SHA256')
        name, digest = item.split('=', 1)
        if not NAME.fullmatch(name) or name in result or not SHA.fullmatch(digest):
            raise ValueError(f'invalid or duplicate tool SHA entry: {name}')
        result[name] = digest
    return result


def validate_tool_sources(sources, expected_hashes, backend_root):
    """Permit Cargo-target artifacts only when a fixed build receipt SHA pins them."""
    result = {}
    for name, path in sources.items():
        digest = sha(path)
        expected = expected_hashes.get(name)
        if expected is not None and digest != expected:
            raise ValueError(f'{name} executor differs from its fixed authority SHA')
        target_source = in_mutable_target(path, backend_root)
        if target_source and expected is None:
            raise ValueError(f'{name} Cargo-target executor requires its fixed build receipt SHA')
        if not target_source and any(part in ('target', '.rust-target') for part in path.parts):
            raise ValueError(f'sealed input path names a mutable build target: {path}')
        result[name] = {'sha256': digest, 'target_source': target_source,
                        'authority_sha256': expected}
    return result


def record_file(path, role, name=None):
    value = {'path': str(path), 'sha256': sha(path), 'role': role}
    if name:
        value['name'] = name
    return value


def backend_source_files(root):
    """Match backend/build.rs's byte-included closure, leaving build outputs out."""
    root = Path(root).resolve(strict=True)
    paths = []
    for relative_root in ('src', 'assets'):
        current = root / relative_root
        if not current.is_dir() or current.is_symlink():
            raise ValueError(f'backend build source directory is missing or linked: {current}')
        for parent, directories, names in os.walk(current, followlinks=False):
            base = Path(parent)
            if any((base / name).is_symlink() for name in directories):
                raise ValueError(f'backend build source tree contains a linked directory: {base}')
            directories.sort()
            for name in sorted(names):
                path = base / name
                if path.is_symlink():
                    raise ValueError(f'backend build source tree contains a linked input: {path}')
                if path.suffix in ('.rs', '.json', '.sql'):
                    paths.append(path.resolve(strict=True))
    for relative in ('schema.sql', 'Cargo.toml', 'Cargo.lock', 'build.rs'):
        path = root / relative
        if relative == 'schema.sql' and not path.exists():
            continue
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'backend build input is missing or linked: {path}')
        paths.append(path.resolve(strict=True))
    return sorted(set(paths), key=lambda path: path.relative_to(root).as_posix())


def backend_source_closure_sha256(records):
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item['name']):
        name = record['name'].encode('utf-8')
        digest.update(len(name).to_bytes(8, 'big'))
        digest.update(name)
        digest.update(bytes.fromhex(record['sha256']))
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-root', type=Path, required=True)
    parser.add_argument('--release-directory', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--tool', action='append', default=[], metavar='NAME=PATH',
                        help='Release executor to copy (repeat for probe/reconcile/clock/spool tools)')
    parser.add_argument('--tool-sha256', action='append', default=[], metavar='NAME=SHA256',
                        help='Fixed C1 build-receipt SHA; mandatory when an executor source is in Cargo target')
    parser.add_argument('--identity-tool', default='probe')
    parser.add_argument('--script', action='append', default=[], type=Path)
    parser.add_argument('--auditor', type=Path, required=True)
    parser.add_argument('--policy-source', type=Path, required=True)
    parser.add_argument('--witness', action='append', default=[], type=Path)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--conflict-policy', default=POLICY)
    args = parser.parse_args(argv)

    backend_root = args.backend_root.expanduser().resolve(strict=True)
    if not backend_root.is_dir():
        raise ValueError('backend root must be an existing directory')
    release_raw = args.release_directory.expanduser()
    if release_raw.is_symlink():
        raise ValueError('release directory cannot be a symlink')
    release = release_raw.resolve()
    manifest = args.manifest.expanduser().resolve()
    if in_mutable_target(release, backend_root) or in_mutable_target(manifest, backend_root):
        raise ValueError('release helpers and seal must live outside mutable Cargo target')
    if release == backend_root or backend_root in release.parents:
        # Keeping the frozen bundle out of the source/backend tree avoids an
        # accidental build input and makes the release path reviewable.
        raise ValueError('release directory must be outside the backend source tree')
    try:
        manifest.relative_to(release)
    except ValueError as error:
        raise ValueError('tool seal must be stored inside its Release directory') from error
    if args.conflict_policy != POLICY:
        raise ValueError('unsupported terminal conflict policy')

    tools = parse_named(args.tool, 'tool')
    tool_hashes = parse_tool_hashes(args.tool_sha256)
    if not {'probe', 'reconcile-probe', 'clock-probe', 'spool-probe'} <= set(tools):
        raise ValueError('probe, reconcile-probe, clock-probe and spool-probe executors are required')
    if not set(tool_hashes) <= set(tools):
        raise ValueError('--tool-sha256 names must refer to supplied --tool entries')
    if args.identity_tool not in tools:
        raise ValueError('identity tool must name one of the copied executors')
    script_paths = [path_file(path, 'sealed script') for path in args.script]
    script_names = {path.name for path in script_paths}
    if len(script_paths) != len(script_names) or REQUIRED_SCRIPTS != script_names:
        raise ValueError('the exact seven terminal scripts, including this sealer, must be supplied once')
    auditor = path_file(args.auditor, 'independent root clock auditor')
    policy_source = path_file(args.policy_source, 'clock policy source')
    witnesses = [path_file(path, 'clock policy witness') for path in args.witness]
    if not witnesses:
        raise ValueError('at least one reviewed clock policy witness is required')
    python = path_file(args.python, 'Python runtime', allow_symlink=True)
    if python != Path(sys.executable).resolve(strict=True):
        raise ValueError('seal builder must run under the exact Python runtime it records')

    sources = {name: path_file(path, f'{name} executor') for name, path in tools.items()}
    source_authority = validate_tool_sources(sources, tool_hashes, backend_root)
    build_sources = backend_source_files(backend_root)
    for path in [*build_sources, *script_paths, auditor, policy_source, *witnesses, python]:
        if in_mutable_target(path, backend_root):
            raise ValueError(f'sealed input points into mutable Cargo target: {path}')
        if any(part in ('target', '.rust-target') for part in path.parts):
            raise ValueError(f'sealed input path names a mutable build target: {path}')

    # Only the policy may share a backend input; its sealed role gets a copy.
    input_paths = [*build_sources, *script_paths, auditor, *witnesses, python]
    if len(input_paths) != len(set(input_paths)) or (
            policy_source in input_paths and policy_source not in build_sources):
        raise ValueError('seal contains duplicate input paths')
    original_policy_source = policy_source
    policy_source_sha256 = sha(original_policy_source)
    policy_copy = release / 'clock-policy-source.rs'
    if policy_copy == manifest or policy_copy in input_paths:
        raise ValueError('policy copy path conflicts with another sealed input')

    if release.exists():
        if not release.is_dir():
            raise ValueError('release path exists and is not a directory')
    else:
        release.mkdir(parents=True, mode=0o700)
    helpers = release / 'helpers'
    helpers.mkdir(mode=0o700, exist_ok=False)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if manifest.exists():
        raise FileExistsError(f'never overwrite an existing tool seal: {manifest}')

    copy_frozen(original_policy_source, policy_copy, policy_source_sha256)
    os.chmod(policy_copy, 0o444)
    policy_source = policy_copy.resolve(strict=True)

    file_records = []
    copied_tools = {}
    for name, source in sources.items():
        if not os.access(source, os.X_OK):
            raise ValueError(f'executor is not executable: {source}')
        destination = helpers / name
        digest = copy_frozen(source, destination, source_authority[name]['sha256'])
        copied_tools[name] = str(destination.resolve(strict=True))
        file_records.append({'path': str(destination.resolve(strict=True)), 'sha256': digest,
                             'role': 'release_executable', 'name': name,
                             'source_path': str(source), 'source_sha256': digest,
                             'source_authority_sha256': source_authority[name]['authority_sha256'],
                             'source_from_mutable_target': source_authority[name]['target_source'],
                             'mode': format(stat.S_IMODE(destination.stat().st_mode), '04o')})

    identities = {}
    for name, raw_path in copied_tools.items():
        executable = Path(raw_path)
        digest = next(item['sha256'] for item in file_records
                      if item.get('role') == 'release_executable' and item.get('name') == name)
        if sha(executable) != digest:
            raise ValueError(f'copied Release helper changed before identity: {name}')
        completed = subprocess.run([str(executable), 'identity'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, timeout=20, check=True)
        report = json.loads(completed.stdout)
        if (report.get('schema') != 'tracefang-migration-tool-identity-v1'
                or not isinstance(report.get('tool'), str) or not report['tool']):
            raise ValueError(f'{name} returned an unsupported executable identity record')
        identities[name] = report
        if sha(executable) != digest:
            raise ValueError(f'copied Release helper changed after identity: {name}')
    identity = identities[args.identity_tool]
    backend_sha = identity.get('backend_build_sha256')
    if not isinstance(backend_sha, str) or not SHA.fullmatch(backend_sha):
        raise ValueError('backend identity must include a full SHA-256 build fingerprint')
    if not isinstance(identity.get('version'), str) or not identity['version']:
        raise ValueError('backend identity must include a package version')
    if any(report.get('backend_build_sha256') != backend_sha or report.get('version') != identity['version']
           for report in identities.values()):
        raise ValueError('Release executables do not share one whole-backend source fingerprint/version')

    # Paths are absolute and the actual file bytes are captured; consumers
    # re-hash every listed input before every phase, including witness files.
    for path in script_paths:
        file_records.append(record_file(path, 'script', path.name))
    source_records = []
    for path in build_sources:
        record = record_file(path, 'backend_source', path.relative_to(backend_root).as_posix())
        source_records.append(record)
        file_records.append(record)
    file_records.append(record_file(auditor, 'independent_clock_auditor', auditor.name))
    file_records.append(record_file(policy_source, 'clock_policy_source', policy_source.name))
    for path in witnesses:
        file_records.append(record_file(path, 'clock_policy_witness', path.name))
    file_records.append({'path': str(python), 'sha256': sha(python), 'role': 'python_runtime',
                         'version': sys.version, 'implementation': sys.implementation.name})

    # Re-check every non-copy source after identity ran so the manifest cannot
    # accidentally combine inputs from different source moments.
    for record in file_records:
        if record['role'] == 'release_executable':
            source = Path(record['source_path'])
            if sha(source) != record['source_sha256']:
                raise ValueError(f'executor source changed during sealing: {source}')
        elif sha(Path(record['path'])) != record['sha256']:
            raise ValueError(f'sealed input changed during sealing: {record["path"]}')

    if sha(original_policy_source) != policy_source_sha256:
        raise ValueError(f'clock policy source changed during sealing: {original_policy_source}')

    seal = {
        'schema': SCHEMA, 'complete': True,
        'conflict_policy_version': args.conflict_policy,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'release_directory': str(release), 'immutable_helper_directory': str(helpers.resolve()),
        'backend_source_root': str(backend_root),
        'backend_source_closure_sha256': backend_source_closure_sha256(source_records),
        'backend_build_sha256': backend_sha, 'backend_version': identity['version'],
        'identity': identity, 'identities': identities, 'identity_tool': args.identity_tool,
        'executables': copied_tools,
        'runtime': {'executable': str(python), 'sha256': sha(python),
                    'version': sys.version, 'implementation': sys.implementation.name},
        'clock_policy': {'policy_id': 'ths-v6-period61-shfe-interval-end-v2',
                         'conflict_policy_version': args.conflict_policy,
                         'source': str(policy_source), 'source_sha256': sha(policy_source),
                         'witnesses': [{'file': str(path), 'sha256': sha(path)} for path in witnesses],
                         'auditor': str(auditor), 'auditor_sha256': sha(auditor)},
        'files': file_records,
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(seal, output, indent=2, sort_keys=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    print(json.dumps({'manifest': str(manifest), 'sha256': sha(manifest),
                      'backend_build_sha256': backend_sha, 'tools': copied_tools,
                      'complete': True}, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f'seal-migration-tools: {error}', file=sys.stderr)
        raise SystemExit(2)
