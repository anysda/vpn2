"""Выпуск: образы по digest, артефакты, журнал деплоя (vpn2.release.state), preflight и опубликованный манифест."""
from contextlib import closing
import gc
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import warnings

from envs import CONFIG, node_env
from vpn2 import release, swap
from vpn2.release import build, state

ROOT = Path(__file__).resolve().parents[3]
HEAD = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                      check=True).stdout.strip()
REPOSITORY = 'anysda/vpn2/panel'
MIB, GIB = 1024 ** 2, 1024 ** 3
A, B, C = ('sha256:' + ch * 64 for ch in 'abc')


def images(component='panel', digest=A, config=A):
    return {'images': {component: {'amd64': {'repository': 'example/' + component, 'digest': digest,
                                             'config': config}}}}


def host(work, **manifest):
    """os-release и meminfo узла в work; манифест под эту машину."""
    (work / 'os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
    (work / 'meminfo').write_text('MemAvailable: 4000000 kB\n')
    return {'os': {'ubuntu': ['26.04']}, 'architectures': [release.architecture()], 'min_free_bytes': 0,
            'min_memory_bytes': 0, **manifest}


def preflight(manifest, work, **options):
    return state.preflight(manifest, work / 'db.sqlite', work, work / 'os-release', work / 'meminfo', **options)


def fake_swapfile(size):
    """os.stat/os.lstat отвечают за /swapfile: файл size байт или его нет (None)."""
    def answer(real):
        def stub(path, *args, **kwargs):
            try:
                name = os.fspath(path)
            except TypeError:
                name = None
            if name not in ('/swapfile', b'/swapfile'):
                return real(path, *args, **kwargs)
            if size is None:
                raise FileNotFoundError(2, 'No such file or directory', '/swapfile')
            return os.stat_result((stat.S_IFREG | 0o600, 4242, 1, 1, 0, 0, size, 0, 0, 0),
                                  {'st_blocks': size // 512, 'st_blksize': 4096})
        return stub
    return patch.multiple(os, stat=answer(os.stat), lstat=answer(os.lstat))


def free_space(free):
    return patch.object(state.shutil, 'disk_usage', lambda _path: type('Usage', (), {'free': free() if callable(free) else free}))


def build_manifest(directory, migrations_after_tag):
    """Выпуск: тег v1.0.0, потом коммиты; возвращает собранный манифест и путь к нему."""
    work = Path(tempfile.mkdtemp(dir=directory))
    repo, folder = work / 'repo', work / 'repo/app/server/database/migrations'
    (folder / 'meta').mkdir(parents=True)
    (repo / 'app/package.json').write_text('{"version": "1.0.1"}')
    git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t']

    def commit(count, message):
        entries = [{'idx': i, 'when': 1000 + i, 'tag': f'{i:04d}_m'} for i in range(count)]
        for entry in entries:
            (folder / (entry['tag'] + '.sql')).write_text('CREATE TABLE t%d(id);' % entry['idx'])
        (folder / 'meta/_journal.json').write_text(json.dumps({'entries': entries}))
        subprocess.run(git[:3] + ['add', '-A'], check=True)
        subprocess.run(git + ['commit', '-q', '--allow-empty', '-m', message], check=True)

    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    commit(2, 'release')
    subprocess.run(git + ['tag', 'v1.0.0'], check=True)
    commit(2 + migrations_after_tag, 'next')
    (repo / 'notes').write_text('later commit')
    commit(2 + migrations_after_tag, 'later')
    output = work / 'release-manifest.json'
    for name in ('panel', 'tgbot', 'emergency'):
        (work / (name + '.txt')).write_text(A)
    argv = ['build-manifest', '--root', str(repo), '--digests', str(work), '--output', str(output)]
    with patch.object(build, 'resolve_image', return_value={}), patch.object(build, 'binary_specs', return_value={}), \
            patch.object(build, 'source_files', return_value={}), patch.object(sys, 'argv', argv):
        build.main()
    return json.loads(output.read_text()), output


class Work(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        # state.main() ставит umask 077 процессу, как команда: следующему тесту - прежний.
        before = os.umask(0)
        os.umask(before)
        self.addCleanup(os.umask, before)


class ImageTests(unittest.TestCase):
    def test_retagged_image_is_rejected_before_panel_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / 'docker').write_text(f'#!/bin/sh\necho \'[{{"Id":"{B}","Architecture":"amd64"}}]\'\n')
            (work / 'docker').chmod(0o755)
            (work / 'release.json').write_text(json.dumps({'format': 1, 'commit': 'a' * 40, 'binaries': {},
                                                           'rollback': 'backward-compatible', **images()}))
            code = "import os\nfrom vpn2.containers import compose\ncompose.release_image(os.environ, 'panel', compose.Docker())"
            run = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=15,
                                 env=dict(os.environ, PATH=f'{work}:{os.environ["PATH"]}', PANEL_IMAGE_PULL='false',
                                          VPN2_RELEASE_MANIFEST=str(work / 'release.json'), VPN2_DEVELOPMENT='false',
                                          PANEL_IMAGE='example/panel:1.0.1'))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('digest', run.stderr)

    def test_matching_offline_image_uses_immutable_id(self):
        with patch.object(release, 'command', return_value=json.dumps([{'Id': A, 'Architecture': 'amd64'}])) as run:
            self.assertEqual(release.image(images(), 'panel', 'example/panel:1.0.1', False, 'amd64'), A)
        self.assertEqual(run.call_count, 1)

    def test_floating_tags_require_development(self):
        for tag in ('dev', 'main', 'latest'):
            with self.assertRaisesRegex(release.ReleaseError, 'development'):
                release.image({}, 'panel', 'example/panel:' + tag)

    def test_registry_timeout_still_requires_matching_local_digest(self):
        answers = [subprocess.TimeoutExpired(['docker', 'pull'], 1200)] * 3 + [
            json.dumps([{'Id': A, 'Architecture': 'amd64'}])]
        with patch.object(release, 'command', side_effect=answers) as run, \
                patch.object(release, 'PULL_PAUSES', (0, 0)), patch('sys.stderr', io.StringIO()):
            self.assertEqual(release.image(images(), 'panel', arch='amd64'), A)
        self.assertEqual(run.call_count, 4)

    def test_containerd_index_selects_platform_from_immutable_id(self):
        index = 'sha256:' + 'd' * 64
        for selected in (A, C):
            with self.subTest(platform_digest=selected):
                responses = [json.dumps([{'Id': index, 'Architecture': 'amd64', 'Descriptor': {
                    'digest': index, 'mediaType': 'application/vnd.oci.image.index.v1+json'}}]),
                    json.dumps([{'Id': selected, 'Architecture': 'amd64', 'RepoDigests': ['example/panel@' + index],
                                 'Descriptor': {'digest': selected}}])]
                with patch.object(release, 'command', side_effect=responses) as run:
                    if selected == A:
                        self.assertEqual(release.image(images(config=B), 'panel', 'example/panel:1.0.1', False, 'amd64'), A)
                    else:
                        with self.assertRaisesRegex(release.ReleaseError, 'digest mismatch'):
                            release.image(images(config=B), 'panel', 'example/panel:1.0.1', False, 'amd64')
                self.assertEqual(run.call_args_list[-1].args[0],
                                 ['docker', 'image', 'inspect', '--platform', 'linux/amd64', index])

    def test_inspect_stores_and_retagged_foreign_images(self):
        for component in ('panel', 'tgbot'):
            repository = 'example/' + component
            reference = repository + '@' + A
            cases = (('classic', B, [reference], None, True), ('containerd', A, [reference], {'digest': A}, True),
                     ('containerd-no-descriptor', A, [reference], None, True), ('classic-loaded', B, [], None, True),
                     ('containerd-loaded', A, [], {'digest': A}, True),
                     ('foreign-classic', C, [repository + '@' + C], None, False),
                     ('foreign-containerd', C, [repository + '@' + C], {'digest': C}, False),
                     ('tag-only', C, [], None, False), ('descriptor-is-not-config', B, [], {'digest': B}, False))
            for label, image_id, repos, descriptor, accepted in cases:
                with self.subTest(component=component, store=label):
                    actual = {'Id': image_id, 'Architecture': 'amd64', 'RepoDigests': repos,
                              'RepoTags': [repository + ':1.0.1'], **({'Descriptor': descriptor} if descriptor else {})}
                    with patch.object(release, 'command', return_value=json.dumps([actual])):
                        if accepted:
                            self.assertEqual(release.image(images(component, config=B), component,
                                                           repository + ':1.0.1', False, 'amd64'), image_id)
                        else:
                            with self.assertRaisesRegex(release.ReleaseError, 'digest mismatch'):
                                release.image(images(component, config=B), component, repository + ':1.0.1',
                                              False, 'amd64')
                    actual['Architecture'] = 'arm64'
                    with patch.object(release, 'command', return_value=json.dumps([actual])), \
                            self.assertRaisesRegex(release.ReleaseError, 'digest mismatch'):
                        release.image(images(component, config=B), component, pull=False, arch='amd64')

    def test_containerd_never_looks_up_unavailable_config(self):
        actual = {'Id': A, 'Architecture': 'amd64', 'RepoDigests': ['example/panel@' + A]}
        with patch.object(release, 'command', return_value=json.dumps([actual])) as run:
            self.assertEqual(release.image(images(config=B), 'panel', pull=False, arch='amd64'), A)
        run.assert_called_once_with(['docker', 'image', 'inspect', 'example/panel@' + A])


class ArtifactTests(Work):
    def test_corrupt_download_rejected_before_copy_or_extract(self):
        expected = hashlib.sha256(b'original archive').hexdigest()
        (self.work / expected).write_bytes(b'corrupted archive')
        destination = self.work / 'install.tar'
        with patch('urllib.request.urlopen', side_effect=lambda *_a, **_k: io.BytesIO(b'corrupted')) as fetch, \
                self.assertRaisesRegex(release.ReleaseError, 'before extraction'):
            release.artifact({'binaries': {'binary': {'url': 'https://example.invalid/a', 'sha256': expected}}},
                             'binary', self.work, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(fetch.call_count, 1)

    def test_corrupt_cache_is_downloaded_again_once(self):
        expected = hashlib.sha256(b'original archive').hexdigest()
        manifest = {'binaries': {'binary': {'url': 'https://example.invalid/a', 'sha256': expected}}}
        cached, destination = self.work / expected, self.work / 'install.tar'
        cached.write_bytes(b'original archivX')
        with patch('urllib.request.urlopen', side_effect=lambda *_a, **_k: io.BytesIO(b'original archive')) as fetch:
            release.artifact(manifest, 'binary', self.work, destination)
        self.assertEqual((fetch.call_count, destination.read_bytes(), cached.read_bytes()),
                         (1, b'original archive', b'original archive'))
        destination.unlink()
        cached.write_bytes(b'original archivX')
        with patch('urllib.request.urlopen', side_effect=lambda *_a, **_k: io.BytesIO(b'mirror broken')) as fetch, \
                self.assertRaises(release.ReleaseError) as caught:
            release.artifact(manifest, 'binary', self.work, destination)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn(str(cached), str(caught.exception))
        self.assertEqual(list(self.work.iterdir()), [])


class JournalTests(Work):
    def manifest(self, *stages):
        os_values = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        manifest = {'format': 1, 'commit': 'a' * 40, 'rollback': 'backward-compatible', 'images': {}, 'binaries': {},
                    'architectures': [release.architecture()], 'min_free_bytes': 0, 'min_memory_bytes': 0,
                    'os': {os_values['ID'].strip('"'): [os_values.get('VERSION_ID', '').strip('"')]},
                    'local_service_api': {'min': 0, 'max': 999}, 'stages': list(stages),
                    'database': {'min': 0, 'max': 0, 'migrate_from': [0, 0], 'journal': []}}
        (self.work / 'manifest.json').write_text(json.dumps(manifest))

    def run_state(self, folder, *arguments, path=None):
        """state.main() над временным состоянием; пути конфигурации хоста не снимаются."""
        argv = ['state', '--state', str(folder), '--database', str(self.work / 'db.sqlite'),
                '--manifest', str(self.work / 'manifest.json'), *arguments]
        original = state.backup
        with patch.object(sys, 'argv', argv), patch.dict(os.environ, PATH=path or os.environ['PATH']), \
                patch.object(state, 'backup', lambda db, destination, _paths: original(db, destination, [])):
            self.assertEqual(state.main(), 0, arguments)

    def test_wal_backup_contains_committed_wal(self):
        database = self.work / 'live.sqlite'
        with closing(sqlite3.connect(database)) as live, live:
            live.execute('PRAGMA journal_mode=WAL')
            live.execute('PRAGMA wal_autocheckpoint=0')
            live.execute('CREATE TABLE access(id INTEGER, allowed INTEGER)')
            live.execute('INSERT INTO access VALUES(1, 0)')
            live.commit()
            self.assertGreater(Path(f'{database}-wal').stat().st_size, 0)
            state.backup(database, self.work / 'snapshot', [])
            with closing(sqlite3.connect(self.work / 'snapshot/db.sqlite')) as copy, copy:
                self.assertEqual(copy.execute('SELECT * FROM access').fetchall(), [(1, 0)])
            state.verify_backup(self.work / 'snapshot')

    def test_successful_deploy_and_panel_transaction_rotate_backups(self):
        keep = state.KEEP_BACKUPS
        with closing(sqlite3.connect(self.work / 'db.sqlite')) as db, db:
            db.execute('CREATE TABLE access(id INTEGER)')
        self.manifest('10-foreign', '99-verify')
        deploy, panel = self.work / 'deploy', self.work / 'deploy/panel'
        (deploy / 'backups/1').mkdir(parents=True)  # остаток недоделанного бэкапа
        for _ in range(keep + 2):
            self.run_state(deploy, 'backup')
            newest = json.loads((deploy / 'operation.json').read_text())['backup']
            self.run_state(deploy, 'phase', '--phase', 'applying')
            self.run_state(deploy, 'complete', '--role', 'exit')
        left = sorted(p.name for p in (deploy / 'backups').iterdir())
        self.assertEqual(len(left), keep, left)
        self.assertIn(Path(newest).name, left)
        # Транзакция панели: два снимка (до окна обслуживания и внутри него).
        panel.mkdir(parents=True, exist_ok=True)
        (panel / 'previous-schema.json').write_text('{}')
        (panel / 'previous-installed.json').write_text('{"release": null}')
        for _ in range(keep + 2):
            self.run_state(panel, 'backup')
            self.run_state(panel, 'backup')
            newest = json.loads((panel / 'operation.json').read_text())['backup']
            self.run_state(panel, 'phase', '--phase', 'complete')
        left = sorted(p.name for p in (panel / 'backups').iterdir())
        self.assertEqual(len(left), keep, left)
        self.assertIn(Path(newest).name, left)
        for name in left:
            state.verify_backup(panel / 'backups' / name)
        self.assertEqual(len(list((deploy / 'backups').iterdir())), keep, 'ротация панели задела бэкапы деплоя')
        self.assertTrue((panel / 'previous-schema.json').is_file())
        self.assertTrue((panel / 'previous-installed.json').is_file())

    def test_exit_release_journal_never_starts_docker(self):
        # Ступень 23 гасит Docker на экзитах, docker.socket поднимает его на любой вызов клиента.
        calls = self.work / 'docker-calls'
        (self.work / 'bin').mkdir()
        (self.work / 'bin/docker').write_text(f'#!/bin/sh\necho "$*" >> {shlex.quote(str(calls))}\necho {C}\n')
        (self.work / 'bin/docker').chmod(0o755)
        self.manifest('10-foreign', '23-emergency', '99-verify')
        path = f'{self.work / "bin"}:{os.environ["PATH"]}'
        for arguments in (['preflight'], ['prepare', '--role', 'exit'], ['backup'], ['phase', '--phase', 'applying'],
                          ['complete', '--role', 'exit'], ['preflight', '--partial'],
                          ['prepare', '--role', 'exit', '--partial']):
            self.run_state(self.work / 'state', *arguments, path=path)
        self.assertFalse(calls.exists(), calls.exists() and calls.read_text())
        # Положительный контроль: entry записывает контейнеры панели через ту же подмену.
        self.run_state(self.work / 'state', 'complete', '--role', 'entry', path=path)
        self.assertIn('inspect --format {{.Image}} anysda-vpn2', calls.read_text())
        installed = json.loads((self.work / 'state/installed.json').read_text())
        self.assertEqual(installed['actual']['images']['anysda-vpn2'], C)

    def test_incompatible_schema_prevents_old_container_start(self):
        database = self.work / 'db.sqlite'
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('CREATE TABLE __drizzle_migrations (hash TEXT, created_at INTEGER)')
            db.execute("INSERT INTO __drizzle_migrations VALUES ('new', 2)")
        with self.assertRaisesRegex(state.ReleaseError, 'old image must not start'):
            state.compatible(database, {'journal': [{'sha256': 'old', 'when': 1}], 'min': 1, 'max': 1})

    def test_journal_closes_sqlite_connections(self):
        with closing(sqlite3.connect(self.work / 'db.sqlite')) as db, db:
            db.execute('CREATE TABLE __drizzle_migrations (hash TEXT, created_at INTEGER)')
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ResourceWarning)
            state.schema(self.work / 'db.sqlite')
            state.backup(self.work / 'db.sqlite', self.work / 'backup', [])
            gc.collect()
        self.assertEqual([str(w.message) for w in caught if w.category is ResourceWarning], [])

    def test_release_with_new_migration_does_not_promise_rollback(self):
        for migrations, rollback in ((1, 'forward-only'), (0, 'backward-compatible')):
            manifest, path = build_manifest(self.work, migrations)
            self.assertEqual((manifest['rollback'], release.load(path)['rollback']), (rollback, rollback))


class PreflightTests(Work):
    def test_disk_space_refusal_names_backup_folders(self):
        for folder, count in (('backups', 2), ('panel/backups', 3)):
            for index in range(count):
                (self.work / folder / str(1000 + index)).mkdir(parents=True)
                (self.work / folder / str(1000 + index) / 'db.sqlite').write_bytes(b'x' * MIB)
        with self.assertRaises(state.ReleaseError) as refused:
            preflight(host(self.work, min_free_bytes=2 ** 62), self.work)
        message = str(refused.exception)
        self.assertIn('services unchanged', message)
        self.assertIn(f'{self.work / "backups"} 2 MiB in 2', message)
        self.assertIn(f'{self.work / "panel/backups"} 3 MiB in 3', message)

    def test_deploy_warns_before_migration_that_closes_rollback(self):
        with closing(sqlite3.connect(self.work / 'db.sqlite')) as db, db:
            db.execute('CREATE TABLE __drizzle_migrations (hash TEXT, created_at INTEGER)')
            db.execute("INSERT INTO __drizzle_migrations VALUES ('a', 1)")
        journal = [{'when': 1, 'sha256': 'a'}, {'when': 2, 'sha256': 'b'}]
        manifest = host(self.work, local_service_api={'min': 1, 'max': 1}, rollback='forward-only')

        def warning(contract, **options):
            manifest['database'] = {'journal': contract, 'min': len(contract), 'max': len(contract),
                                    'migrate_from': [0, len(contract)]}
            with patch('sys.stderr', new_callable=io.StringIO) as err:
                preflight(manifest, self.work, **options)
            return err.getvalue()

        text = warning(journal)
        self.assertIn('rollback', text)
        self.assertIn('1 -> 2', text)
        self.assertEqual(warning(journal[:1]), '', 'та же схема сохраняет откат молча')
        self.assertEqual(warning(journal, warn=False), '', 'prepare повторяет предупреждение preflight')

    def swap_refusal(self, swap_size):
        (self.work / 'backups/1000').mkdir(parents=True)
        (self.work / 'backups/1000/db.sqlite').write_bytes(b'x' * MIB)
        with fake_swapfile(swap_size), self.assertRaises(state.ReleaseError) as refused:
            preflight(host(self.work, min_free_bytes=2 ** 62), self.work)
        return str(refused.exception), state.backup_usage(self.work)

    def test_refusal_names_swapfile_size_and_swapoff(self):
        message, _ = self.swap_refusal(1962 * MIB)
        for part in ('services unchanged', 'deploy backups:', '/swapfile', '1962', 'swapoff'):
            self.assertIn(part, message)

    def test_refusal_without_swapfile_unchanged(self):
        message, backups = self.swap_refusal(None)
        self.assertEqual(message, 'insufficient disk space before backup; services unchanged; ' + backups)


class SwapAfterCleanInstallTests(Work):
    """Entry 2 ГБ RAM / 10 ГБ диска сразу после первой установки, следующий ./deploy.sh.

    Узел: 9978 МиБ на ФС, из них 527 резерв ext4; чистая ОС 2000 МиБ, после установки 5556 МиБ.
    """
    USABLE, CLEAN, INSTALLED, INSTALL_SIZE = (9978 - 527) * MIB, 2000 * MIB, 5556 * MIB, 6 * GIB

    def swap_size(self, install_bytes):
        return swap.plan(2014000, self.USABLE - self.CLEAN, 2 * GIB, swap.pending(install_bytes, self.CLEAN))[0]

    def refusal(self, free, swap_size=None, freed=0, entry=True):
        """(текст отказа preflight или '', вызовы swap.release) при свободном месте free."""
        space, calls = [free], []

        def give_back(min_free, say, *, proc, install_bytes):
            calls.append((min_free, str(proc) == str(self.work), install_bytes))
            if freed:
                say('swap: stub')
                space[0] += freed
            return bool(freed)

        if entry:
            (self.work / 'db.sqlite').write_bytes(b'')
        manifest = host(self.work, min_free_bytes=2 * GIB, database={}, local_service_api={'min': 0, 'max': 2 ** 31})
        with free_space(lambda: space[0]), fake_swapfile(swap_size), patch.object(state.swap, 'release', give_back), \
                patch.object(state, 'compatible', lambda *_a, **_k: None):
            try:
                preflight(manifest, self.work, warn=False)
            except state.ReleaseError as refused:
                return str(refused), calls
        return '', calls

    def test_old_swap_size_is_refused_on_the_second_deploy(self):
        self.assertIn('insufficient disk space before backup',
                      self.refusal(self.USABLE - self.INSTALLED - self.swap_size(0))[0])

    def test_swap_sized_below_the_install_lets_the_second_deploy_through(self):
        size = self.swap_size(self.INSTALL_SIZE)
        self.assertEqual(size, 747 * MIB)
        self.assertEqual(self.refusal(self.USABLE - self.INSTALLED - size)[0], '')

    def test_backups_may_grow_up_to_the_limit_and_not_a_byte_over(self):
        free = self.USABLE - self.INSTALLED - self.swap_size(self.INSTALL_SIZE)
        room = free - 2 * GIB
        self.assertGreaterEqual(room, 512 * MIB)
        self.assertEqual(self.refusal(free - room)[0], '')
        self.assertIn('insufficient disk space before backup', self.refusal(free - room - 1)[0])

    # Entry с v1.1.5 после обновления: 1807 МиБ свободно, включён /swapfile 1962 МиБ.
    def test_stuck_entry_goes_on_after_swap_gave_the_room_back(self):
        self.assertEqual(self.refusal(1807 * MIB, 1962 * MIB, (1962 - 747) * MIB),
                         ('', [(2 * GIB, True, 6 * GIB)]))

    def test_exit_has_no_install_to_keep_room_for(self):
        self.assertEqual(self.refusal(1807 * MIB, 1962 * MIB, (1962 - 747) * MIB, entry=False),
                         ('', [(2 * GIB, True, 0)]))

    def test_swap_that_could_not_be_healed_keeps_the_refusal(self):
        refusal, calls = self.refusal(1807 * MIB, 1962 * MIB)
        self.assertEqual(len(calls), 1)
        self.assertIn('insufficient disk space before backup; services unchanged', refusal)
        self.assertIn('/swapfile 1962 MiB', refusal)

    def test_room_given_back_but_still_too_little_is_refused(self):
        self.assertIn('insufficient disk space before backup', self.refusal(500 * MIB, 1962 * MIB, 1215 * MIB)[0])

    def test_enough_room_leaves_swap_alone(self):
        self.assertEqual(self.refusal(2 * GIB, 1962 * MIB), ('', []))

    def test_swap_state_is_read_next_to_meminfo_never_from_the_machine_running_the_test(self):
        self.assertFalse(swap.release(2 ** 62, self.fail, proc=self.work, install_bytes=0))


class Registry:
    """Часть OCI distribution, которую GHCR отдаёт анонимно."""

    def __init__(self, blob=None, layer_digest=None, stalls=0):
        self.objects, self.requests, self.stalls = {}, [], stalls
        if blob is not None:
            digest = layer_digest or 'sha256:' + hashlib.sha256(blob).hexdigest()
            self.objects[f'/v2/{REPOSITORY}/blobs/{digest}'] = blob
            self.objects[f'/v2/{REPOSITORY}/manifests/release-{HEAD}'] = json.dumps({
                'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                'artifactType': 'application/vnd.anysda.vpn2.release.v1+json',
                'config': {'mediaType': 'application/vnd.oci.empty.v1+json', 'size': 2,
                           'digest': 'sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'},
                'layers': [{'mediaType': release.MANIFEST_MEDIA_TYPE, 'digest': digest, 'size': len(blob)}]}).encode()
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                registry.requests.append(self.path)
                if self.path.startswith('/token?'):
                    return self.reply(200, b'{"token": "anonymous"}')
                if self.headers.get('Authorization') != 'Bearer anonymous':
                    self.send_response(401)
                    self.send_header('WWW-Authenticate', f'Bearer realm="{registry.url}/token",'
                                     f'service="test",scope="repository:{REPOSITORY}:pull"')
                    self.end_headers()
                    return None
                if '/blobs/' in self.path and registry.stalls:
                    registry.stalls -= 1
                    time.sleep(1)
                body = registry.objects.get(self.path)
                return self.reply(200, body) if body is not None else self.reply(404, b'{}')

            def reply(self, code, body):
                self.send_response(code)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.source = f'{self.url}/{REPOSITORY}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def contract(commit=HEAD, stages=None):
    return {'format': 1, 'version': '0.0.0-test', 'commit': commit, 'rollback': 'forward-only', 'images': {},
            'binaries': {}, 'stages': release.source_files(ROOT) if stages is None else stages}


class PublishedManifestTests(Work):
    """./deploy.sh без флагов: проверенный выпуск - опубликованный манифест commit дерева, без gh."""

    def registry(self, *args, **kwargs):
        registry = Registry(*args, **kwargs)
        self.addCleanup(registry.close)
        return registry

    def cli(self, *args, source, manifest='release-manifest.json'):
        return subprocess.run([sys.executable, '-m', 'vpn2.release', '--manifest', str(self.work / manifest), *args,
                               str(ROOT)], env=dict(os.environ, VPN2_RELEASE_SOURCE=source),
                              capture_output=True, text=True, timeout=60)

    def test_unpublished_commit_explains_what_to_do(self):
        result = self.cli('fetch', source=self.registry().source)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'манифест commit {HEAD[:12]} не опубликован', result.stderr)
        self.assertIn('./deploy.sh --development', result.stderr)
        self.assertNotIn('gh ', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_unreachable_publication_explains_network_and_offline_path(self):
        result = self.cli('fetch', source='http://127.0.0.1:9/' + REPOSITORY)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'не удалось скачать манифест commit {HEAD[:12]}', result.stderr)
        self.assertIn('без сети: ./deploy.sh --manifest <файл>', result.stderr)

    def test_published_manifest_is_fetched_anonymously_and_verified(self):
        registry = self.registry(json.dumps(contract()).encode())
        self.assertEqual(self.cli('fetch', source=registry.source).returncode, 0)
        self.assertIn('/token?', ''.join(registry.requests))
        check = self.cli('verify-tree', source=registry.source)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn(f'манифест commit {HEAD} (версия 0.0.0-test) проверен', check.stdout)

    def test_hanging_registry_connection_is_retried_quickly(self):
        for stalls, fetched in ((2, True), (3, False)):
            registry, out = self.registry(json.dumps(contract()).encode(), stalls=stalls), self.work / f'{stalls}.json'
            with patch.object(release, 'REGISTRY_TIMEOUT', 0.3):
                if fetched:
                    release.fetch(ROOT, out, registry.source)
                    self.assertEqual(json.loads(out.read_text())['commit'], HEAD)
                else:
                    with self.assertRaisesRegex(release.ReleaseError, 'не удалось скачать манифест'):
                        release.fetch(ROOT, out, registry.source)

    def test_cached_manifest_of_same_commit_survives_network_loss(self):
        (self.work / 'release-manifest.json').write_text(json.dumps(contract()))
        result = self.cli('fetch', source='http://127.0.0.1:9/x')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('скачанный раньше', result.stderr)
        (self.work / 'release-manifest.json').write_text(json.dumps(contract(commit='0' * 40)))
        self.assertEqual(self.cli('fetch', source='http://127.0.0.1:9/x').returncode, 1)

    def test_tampered_publication_is_rejected(self):
        for registry, message in ((self.registry(json.dumps(contract()).encode(), layer_digest=A), 'повреждён'),
                                  (self.registry(json.dumps(contract(commit='1' * 40)).encode()),
                                   'манифест другого commit')):
            result = self.cli('fetch', source=registry.source, manifest='rejected.json')
            self.assertEqual(result.returncode, 1)
            self.assertIn(message, result.stderr)
            self.assertFalse((self.work / 'rejected.json').exists())

    def test_changed_stage_names_the_file(self):
        stages = release.source_files(ROOT)
        name = next(iter(stages))
        stages[name] = '0' * 64
        (self.work / 'release-manifest.json').write_text(json.dumps(contract(stages=stages)))
        result = self.cli('verify-tree', source='')
        self.assertEqual(result.returncode, 1)
        self.assertIn(name, result.stderr)
        self.assertIn('git stash', result.stderr)

    def test_image_from_config_only_in_development(self):
        text = CONFIG + '\npanel:\n  image: ghcr.io/anysda/vpn2/panel:1.0.0\ntgbot:\n  image: ghcr.io/anysda/vpn2/tgbot:1.0.0\n'
        for development, expected in (('false', (None, None)),
                                      ('true', ('ghcr.io/anysda/vpn2/panel:1.0.0', 'ghcr.io/anysda/vpn2/tgbot:1.0.0'))):
            prepared, env = node_env(self.work, text, env={'VPN2_DEVELOPMENT': development})
            self.assertIsNotNone(env, prepared.stderr)
            self.assertEqual((env.get('PANEL_IMAGE'), env.get('TGBOT_IMAGE')), expected)

    def test_readme_install_needs_no_manual_release_steps(self):
        install = (ROOT / 'README.md').read_text().split('# 🚀 Quickstart', 1)[1].split('\n## ', 2)
        install = install[0] + install[1]  # Quickstart и «Настройка»
        for forbidden in ('gh ', 'gh auth', 'gh run', '--manifest', 'release-manifest'):
            self.assertNotIn(forbidden, install)
        steps = [install.index(step) for step in ('git clone', '\n./setup.sh\n', '\n./deploy.sh\n')]
        self.assertIn('ssh-copy-id', install)
        self.assertEqual(steps, sorted(steps))
