#!/usr/bin/env python3
"""Host-side deploy lock, preflight, WAL-safe snapshots and compatibility checks."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

from release import ReleaseError, architecture, artifact, command, image, load, read_json, sha256


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def schema(database):
    if not Path(database).exists():
        return []
    with sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if '__drizzle_migrations' not in tables:
            if tables:
                raise ReleaseError('unrecognised database: migration history missing')
            return []
        return [{'when': row[0], 'sha256': row[1]} for row in connection.execute(
            'SELECT created_at, hash FROM __drizzle_migrations ORDER BY created_at')]


def compatible(database, contract, migrating=False):
    actual = schema(database)
    expected = contract['journal']
    if actual != expected[:len(actual)]:
        raise ReleaseError('incompatible database history; old image must not start')
    low, high = contract['migrate_from'] if migrating else (contract['min'], contract['max'])
    if not low <= len(actual) <= high:
        raise ReleaseError('incompatible database schema; old image must not start')


def rollback_warning(database, manifest):
    """Old images refuse unknown migrations: say so before the deploy changes anything."""
    applied, target = len(schema(database)), len(manifest['database']['journal'])
    if applied and applied < target:
        print(f'WARNING: this release migrates the panel database {applied} -> {target}; '
              'after it ./deploy.sh rollback to the installed release is impossible '
              f"(release rollback plan: {manifest['rollback']}). Return path: a later compatible release, "
              'or restore the pre-deploy backup and lose changes made after it.', file=sys.stderr)


def image_contract(reference):
    """Read the actual immutable image, including legacy images without labels."""
    container = command(['docker', 'create', '--network', 'none', reference]).strip()
    try:
        with tempfile.TemporaryDirectory(prefix='vpn2-image-') as temporary:
            command(['docker', 'cp', container + ':/app/server/database/migrations', temporary])
            folder = Path(temporary) / 'migrations'
            entries = read_json(folder / 'meta/_journal.json')['entries']
            return {'min': len(entries), 'max': len(entries), 'migrate_from': [0, len(entries)],
                    'journal': [{'when': e['when'], 'sha256': sha256(folder / (e['tag'] + '.sql'))} for e in entries]}
    finally:
        command(['docker', 'rm', container])


def composition(role='entry'):
    packages = command(['dpkg-query', '-W', '-f=${Package}\t${Version}\n']).splitlines()
    binaries = {}
    for name, path in {'sing-box': '/usr/local/bin/sing-box', 'wgcf': '/usr/local/bin/wgcf',
                       'caddy': '/usr/bin/caddy', 'node-exporter': '/usr/local/bin/node_exporter',
                       'adguard': '/opt/AdGuardHome/AdGuardHome', 'nfqws2': '/opt/zapret2/binaries/my/nfqws2'}.items():
        if Path(path).is_file():
            binaries[name] = sha256(path)
    images = {}
    # Panel, bot and metrics run only on entry. On an exit docker.socket would
    # start dockerd just to answer, and it stays running after stage 23.
    if role == 'entry' and shutil.which('docker'):
        for name in ('anysda-vpn2', 'anysda-tgbot', 'vmsingle'):
            result = subprocess.run(['docker', 'inspect', '--format', '{{.Image}}', name],
                                    capture_output=True, text=True, timeout=30)
            images[name] = result.stdout.strip() if result.returncode == 0 else None
    return {'packages': packages, 'binaries': binaries, 'images': images}


def backup(database, destination, paths):
    """Never mutate working state; partial snapshots are never marked complete."""
    destination = Path(destination)
    destination.mkdir(parents=True, mode=0o700)
    checksums = {}
    if Path(database).exists():
        copy = destination / 'db.sqlite'
        with sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(copy) as target:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ReleaseError('backup integrity check failed')
        os.chmod(copy, 0o600)
        checksums['db.sqlite'] = sha256(copy)
    archive = destination / 'configuration.tar'
    with tarfile.open(archive, 'w') as output:
        for path in paths:
            if Path(path).exists():
                output.add(path, arcname=str(path).lstrip('/'), recursive=True)
    os.chmod(archive, 0o600)
    with tarfile.open(archive, 'r') as reader:
        for member in reader:
            if member.isfile():
                with reader.extractfile(member) as stream:
                    while stream.read(1024 * 1024):
                        pass
    checksums['configuration.tar'] = sha256(archive)
    atomic_json(destination / 'complete.json', checksums)
    verify_backup(destination)


def verify_backup(destination):
    destination = Path(destination)
    for name, expected in read_json(destination / 'complete.json').items():
        if sha256(destination / name) != expected:
            raise ReleaseError('backup checksum failed')
    database = destination / 'db.sqlite'
    if database.exists():
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
            if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ReleaseError('backup is not readable')


KEEP_BACKUPS = 3


def backup_dirs(folder):
    """Snapshots by creation time, newest first; only names written by backup."""
    if not folder.is_dir():
        return []
    return sorted((item for item in folder.iterdir() if item.is_dir() and item.name.isdigit()),
                  key=lambda item: int(item.name), reverse=True)


def rotate_backups(state, keep=KEEP_BACKUPS):
    """After a successful operation keep the newest complete snapshots.

    The snapshot of the operation just finished is never removed, even if newer
    incomplete directories exist; incomplete leftovers of failed attempts go.
    """
    folder = Path(state) / 'backups'
    journal = Path(state) / 'operation.json'
    current = read_json(journal).get('backup') if journal.exists() else None
    kept, removed = 0, 0
    for item in backup_dirs(folder):
        if str(item) == current or ((item / 'complete.json').exists() and kept < keep):
            kept += 1
            continue
        try:
            shutil.rmtree(item)
        except OSError as error:
            # The operation already succeeded: cleanup must not turn it into a failure.
            print(f'WARNING: old deploy backup {item} not removed: {type(error).__name__}', file=sys.stderr)
            continue
        removed += 1
    if removed:
        print(f'old deploy backups removed: {removed}; kept {kept} newest in {folder}')


def backup_usage(state):
    """Where deploy snapshots are and how much they take, for the disk-space error."""
    parts = []
    for folder in (Path(state) / 'backups', Path(state) / 'panel/backups'):
        items = backup_dirs(folder)
        if not items:
            continue
        size = 0
        for item in items:
            for root, _dirs, files in os.walk(item):
                for name in files:
                    try:
                        size += (Path(root) / name).lstat().st_size
                    except OSError:
                        pass
        parts.append(f'{folder} ' + (f'{size / 2 ** 30:.1f} GiB' if size >= 2 ** 30 else f'{size // 2 ** 20} MiB') +
                     f' in {len(items)}')
    if not parts:
        return 'deploy backups: none'
    return ('deploy backups: ' + ', '.join(parts) +
            f'; each successful deploy keeps the {KEEP_BACKUPS} newest, older ones can be removed')


def preflight(manifest, database, state, os_release=Path('/etc/os-release'), meminfo=Path('/proc/meminfo'), warn=True):
    os_values = dict(line.split('=', 1) for line in os_release.read_text().splitlines() if '=' in line)
    distro = os_values.get('ID', '').strip('"')
    version = os_values.get('VERSION_ID', '').strip('"')
    if version not in manifest['os'].get(distro, []):
        raise ReleaseError('unsupported operating system')
    if architecture() not in manifest['architectures']:
        raise ReleaseError('unsupported architecture')
    if shutil.disk_usage(state).free < manifest['min_free_bytes']:
        raise ReleaseError('insufficient disk space before backup; services unchanged; ' + backup_usage(state))
    memory = dict(line.split(':', 1) for line in meminfo.read_text().splitlines())
    if int(memory['MemAvailable'].split()[0]) * 1024 < manifest['min_memory_bytes']:
        raise ReleaseError('insufficient available memory')
    compatible(database, manifest['database'], migrating=True)
    if warn:
        rollback_warning(database, manifest)
    api = Path('/etc/vpn2/control-api-version')
    if api.exists():
        version = int(api.read_text())
        required = manifest['local_service_api']
        if not required['min'] <= version <= required['max']:
            raise ReleaseError('local service API is incompatible')


def hold_lock(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'deploy.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError('deploy already running') from exc
        journal = state / 'operation.json'
        if journal.exists() and read_json(journal)['phase'] not in ('complete', 'aborted', 'rolled-back'):
            raise ReleaseError('unfinished deploy; inspect operation.json and run deploy recovery before retry')
        print('locked', flush=True)
        # EOF from a disconnected orchestrator releases flock. The journal stays.
        sys.stdin.readline()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path('/var/lib/vpn2-deploy'))
    parser.add_argument('--database', type=Path, default=Path('/var/lib/anysda-vpn2/db.sqlite'))
    parser.add_argument('--manifest', default='/tmp/anysda/release-manifest.json')
    parser.add_argument('action', choices=('hold', 'preflight', 'prepare', 'backup', 'phase', 'complete', 'compatible', 'image-contract', 'record-rollback'))
    parser.add_argument('--image')
    parser.add_argument('--contract', type=Path)
    parser.add_argument('--migrating', action='store_true')
    parser.add_argument('--partial', action='store_true')
    parser.add_argument('--panel-image', default='')
    parser.add_argument('--bot-image', default='')
    parser.add_argument('--offline-images', action='store_true')
    parser.add_argument('--phase', default='prepared')
    parser.add_argument('--role', choices=('entry', 'exit'), default='entry')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == 'hold':
            hold_lock(args.state)
            return 0
        args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.action == 'image-contract':
            atomic_json(args.contract, image_contract(args.image))
            return 0
        if args.action == 'compatible' and args.contract:
            compatible(args.database, read_json(args.contract), args.migrating)
            print('database: compatible')
            return 0
        if args.action == 'record-rollback':
            previous = args.state / 'panel/previous-installed.json'
            record = read_json(previous) if previous.exists() else {'release': None}
            record['actual'] = composition()
            record['state'] = 'panel-rollback'
            atomic_json(args.state / 'installed.json', record)
            return 0
        manifest = load(args.manifest) if args.action in ('preflight', 'prepare', 'complete', 'compatible') else None
        journal = args.state / 'operation.json'
        if args.action in ('preflight', 'prepare'):
            # prepare repeats the checks right after preflight: warn once.
            preflight(manifest, args.database, args.state, warn=args.action == 'preflight')
            if args.partial:
                installed = args.state / 'installed.json'
                previous = read_json(installed).get('release') if installed.exists() else None
                if not previous or previous['stages'] != manifest['stages']:
                    raise ReleaseError('release stages changed; run full deploy before individual stages')
            if args.action == 'preflight':
                print('OS, resources and compatibility: verified')
                return 0
            if args.role == 'entry':
                image(manifest, 'panel', args.panel_image, not args.offline_images)
                image(manifest, 'tgbot', args.bot_image, not args.offline_images)
            for name, item in manifest['binaries'].items():
                if item['architecture'] in ('all', architecture()):
                    artifact(manifest, name, '/var/cache/vpn2-release')
            print('preflight and artifacts: verified; services unchanged')
        elif args.action == 'backup':
            destination = args.state / 'backups' / str(time.time_ns())
            paths = [Path(p) for p in ('/etc/anysda', '/etc/wireguard', '/etc/openvpn', '/etc/strongswan',
                                      '/etc/swanctl', '/etc/sing-box', '/etc/caddy', '/opt/AdGuardHome/AdGuardHome.yaml',
                                      '/var/lib/anysda-vpn2/runtime', '/var/lib/vpn2-control')]
            backup(args.database, destination, paths)
            atomic_json(journal, {'phase': 'backed-up', 'backup': str(destination)})
            print('backup: SQLite WAL, PKI and configuration verified; working data unchanged')
        elif args.action == 'compatible':
            compatible(args.database, manifest['database'])
            print('database: compatible')
        elif args.action == 'phase':
            operation = read_json(journal)
            operation['phase'] = args.phase
            atomic_json(journal, operation)
            if args.phase == 'complete':
                rotate_backups(args.state)
        else:
            if args.role == 'entry':
                compatible(args.database, manifest['database'])
            atomic_json(args.state / 'installed.json', {'release': manifest, 'actual': composition(args.role)})
            operation = read_json(journal)
            operation['phase'] = 'complete'
            atomic_json(journal, operation)
            rotate_backups(args.state)
            print('installed composition: recorded; OS security updates remain enabled')
    except (ReleaseError, OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
        if args.action == 'backup':
            message = 'mandatory backup failed; no migration permitted; check backup storage and retry'
        else:
            message = str(error) if isinstance(error, ReleaseError) else type(error).__name__
        print('deploy stopped: ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
