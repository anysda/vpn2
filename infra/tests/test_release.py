"""Release acceptance checks. VPN2_TEST_ROOT selects the unchanged baseline."""
from contextlib import closing
import gc
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import warnings


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


def module(name):
    path = ROOT / 'infra/lib' / (name + '.py')
    if not path.exists():
        raise AssertionError(f'{name}: deploy has no release safety mechanism')
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class ReleaseTests(unittest.TestCase):
    def test_each_exit_bootstrap_waits_for_previous_exit_update(self):
        source = (ROOT / 'infra/deploy.sh').read_text()
        functions = ''
        for name in ('do_all', 'run_tracks'):
            functions += name + '() {' + source.split(name + '() {', 1)[1].split('\n}', 1)[0] + '\n}\n'
        script = functions + '''
for name in print_banner pause_entry_auto_updates install_prereqs do_check preflight_ssh verify_and_rotate_ports print_summary announce_deploy_done release_complete; do
  eval "$name() { :; }"
done
die() { exit 1; }
expand_hosts() { case "$1" in all) echo 'ru alpha beta';; foreign) echo 'alpha beta';; *) echo "$1";; esac; }
run_stage() { for host in $(expand_hosts "$2"); do echo "$1 $host"; done; }
prefetch_images() { true & _PREFETCH_PID=$!; }
do_all
'''
        run = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        stages = [line for line in run.stdout.splitlines() if line.endswith((' alpha', ' beta'))]
        self.assertEqual(stages[:6], ['00-bootstrap alpha', '05-mgmt-mesh alpha', '10-foreign alpha',
                                      '00-bootstrap beta', '05-mgmt-mesh beta', '10-foreign beta'])

    def test_inventory_failure_never_announces_success(self):
        source = (ROOT / 'infra/deploy.sh').read_text()
        script = 'do_all() {' + source.split('do_all() {', 1)[1].split('\n}', 1)[0] + '\n}\n'
        script += '''
for name in print_banner pause_entry_auto_updates install_prereqs do_check preflight_ssh verify_and_rotate_ports run_stage run_tracks; do
  eval "$name() { :; }"
done
expand_hosts() { echo exit; }
prefetch_images() { true & _PREFETCH_PID=$!; }
release_complete() { return 1; }
print_summary() { echo premature-success; }
announce_deploy_done() { echo premature-notification; }
do_all
'''
        run = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(run.returncode, 0, 'failed inventory was announced as successful deploy')
        self.assertNotIn('premature-', run.stdout)

    def test_previous_hosts_are_read_from_retained_container(self):
        source = (ROOT / 'infra/scripts/30-frontend.sh').read_text()
        capture = 'PREVIOUS_NODE_HOSTS=' + source.split('PREVIOUS_NODE_HOSTS=', 1)[1].split('\ndocker rm', 1)[0]
        run = subprocess.run(['bash', '-euo', 'pipefail', '-c', '''
docker() {
  [[ "${*: -1}" == anysda-vpn2-previous ]] || return 1
  echo 'NUXT_NODE_HOSTS=exit:example.invalid'
}
''' + capture + '\nprintf "%s" "$PREVIOUS_NODE_HOSTS"'], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, 'exit:example.invalid')

    def test_recovery_rechecks_contract_and_backups_before_resuming(self):
        library = ROOT / 'infra/lib/deploy-release.sh'
        self.assertTrue(library.exists(), 'release recovery is missing')
        run = subprocess.run(['bash', '-euo', 'pipefail', '-c', f'''
source {shlex.quote(str(library))}
DEPLOY_ROOT={shlex.quote(str(ROOT / 'infra'))}
VPN2_RELEASE_MANIFEST=release-manifest.json
install_prereqs() {{ :; }}
load_env() {{ :; }}
expand_hosts() {{ echo ru; }}
push() {{ :; }}
python3() {{ echo source-checked; }}
die() {{ echo "$*" >&2; exit 1; }}
ssh_exec() {{
  if [[ "$*" == *'exec flock'* ]]; then echo locked; read -r unlock
  elif [[ "$*" == *'unfinished deploy'* || "$*" == *--partial* ]]; then return 1
  elif [[ "$*" == *preflight* ]]; then echo preflight-checked
  elif [[ "$*" == *prepare* ]]; then echo artifacts-checked
  elif [[ "$*" == *' backup'* ]]; then echo backup-checked
  fi
}}
release_start recover
release_complete
'''], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.splitlines(), ['source-checked', 'preflight-checked', 'artifacts-checked', 'backup-checked'])

    def test_exit_completion_reloads_files_removed_by_stage_cleanup(self):
        library = ROOT / 'infra/lib/deploy-release.sh'
        self.assertTrue(library.exists(), 'release orchestration is missing')
        run = subprocess.run(['bash', '-euo', 'pipefail', '-c', f'''
source {shlex.quote(str(library))}
DEPLOY_ROOT={shlex.quote(str(ROOT / 'infra'))}
VPN2_DEPLOY_MUTATING=true
VPN2_RELEASE_MANIFEST=release-manifest.json
helper=false manifest=false
expand_hosts() {{ echo exit; }}
load_env() {{ :; }}
push() {{
  if [[ "$1" == *deploy_state.py ]]; then helper=true; fi
  if [[ "$1" == release-manifest.json ]]; then manifest=true; fi
}}
ssh_exec() {{ [[ "$helper" == true && "$manifest" == true ]]; }}
release_complete
'''], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_corrupt_wgcf_never_replaces_working_executable(self):
        source = (ROOT / 'infra/scripts/10-foreign.sh').read_text()
        block = "WGCF_VER=" + source.split('WGCF_VER=', 1)[1].split('\ncd /etc/anysda', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = root / 'wgcf'
            previous.write_bytes(b'previous verified executable')
            script = '''
release_download() { printf corrupted > "$2"; }
curl() { while [[ "$1" != -o ]]; do shift; done; printf corrupted > "$2"; }
''' + block.replace('/usr/local/bin/', directory + '/')
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, 'corrupt wgcf was installed')
            self.assertEqual(previous.read_bytes(), b'previous verified executable')
            self.assertEqual(list(root.glob('.wgcf.*')), [])

    def test_preflight_does_not_wait_for_lifetime_lock(self):
        source = (ROOT / 'infra/deploy.sh').read_text()
        body = source.split('preflight_ssh() {', 1)[1].split('\n}', 1)[0]
        script = 'preflight_ssh() {' + body + '\n}\n' + '''
C_B= C_END= C_Y= C_R=
expand_hosts() { echo ru; }
_preflight_host() { touch "$4/$1.ok"; }
sleep 3 & lifetime=$!
trap 'kill "$lifetime" 2>/dev/null || true; wait "$lifetime" 2>/dev/null || true' EXIT
preflight_ssh
'''
        started = time.monotonic()
        run = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertLess(time.monotonic() - started, 2, 'preflight waited for the lifetime deploy lock')

    def test_local_lock_precedes_environment_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / 'deploy.sh', root / 'deploy.sh')
            (root / 'config.yaml').touch()
            (root / 'infra/lib').mkdir(parents=True)
            (root / 'infra/lib/config2env.py').write_text(
                'from pathlib import Path\nimport sys\n'
                'with (Path(sys.argv[1]) / "generated").open("a") as output: output.write("generated\\n")\n')
            stage = root / 'infra/deploy.sh'
            stage.write_text('#!/bin/sh\nsleep 2\n')
            stage.chmod(0o755)
            first = subprocess.Popen(['bash', str(root / 'deploy.sh')], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if (root / 'generated').exists():
                        break
                    time.sleep(0.01)
                second = subprocess.run(['bash', str(root / 'deploy.sh')], capture_output=True, text=True, timeout=5)
                self.assertNotEqual(second.returncode, 0, 'concurrent deploy rewrote the active env files')
                self.assertEqual((root / 'generated').read_text().splitlines(), ['generated'])
            finally:
                first.communicate(timeout=5)

    def test_clean_orchestrator_installs_transport_before_remote_lock(self):
        library = ROOT / 'infra/lib/deploy-release.sh'
        self.assertTrue(library.exists(), 'release orchestration is missing')
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'sshpass-installed'
            run = subprocess.run(['bash', '-euo', 'pipefail', '-c', f'''
source {shlex.quote(str(library))}
DEPLOY_ROOT={shlex.quote(str(ROOT / 'infra'))}
VPN2_DEVELOPMENT=true
install_prereqs() {{ touch {shlex.quote(str(marker))}; }}
load_env() {{ :; }}
expand_hosts() {{ echo ru; }}
push() {{ :; }}
die() {{ echo "$*" >&2; exit 1; }}
ssh_exec() {{
  test -f {shlex.quote(str(marker))} || return 127
  if [[ "$*" == *'exec flock'* ]]; then echo locked; read -r unlock; fi
}}
release_start
release_complete
'''], capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_deploy_lock_and_interrupted_operation(self):
        program = ROOT / 'infra/lib/deploy_state.py'
        self.assertTrue(program.exists(), 'deploy has no installation lock')
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, str(program), '--state', directory, 'hold']
            first = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(first.stdout.readline().strip(), 'locked')
                second = subprocess.run(command, input='', capture_output=True, text=True, timeout=10)
                self.assertNotEqual(second.returncode, 0)
                self.assertIn('deploy already running', second.stderr)
            finally:
                first.stdin.close()
                first.wait(timeout=10)
                first.stdout.close()
                first.stderr.close()
            (Path(directory) / 'operation.json').write_text('{"phase":"applying"}')
            interrupted = subprocess.run(command, input='', capture_output=True, text=True, timeout=10)
            self.assertNotEqual(interrupted.returncode, 0)
            self.assertIn('unfinished deploy', interrupted.stderr)
            print('deploy lock: second executor rejected; interrupted operation requires recovery')

    def test_retagged_image_is_rejected_before_panel_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            tools = work / 'bin'
            tools.mkdir()
            fake = tools / 'docker'
            fake.write_text('#!/bin/sh\necho \'[{"Id":"sha256:' + 'b' * 64
                            + '","Architecture":"amd64"}]\'\n')
            fake.chmod(0o755)
            manifest = {'format': 1, 'commit': 'a' * 40, 'rollback': 'backward-compatible',
                        'images': {'panel': {'amd64': {'repository': 'example/panel',
                                                     'digest': 'sha256:' + 'a' * 64,
                                                     'config': 'sha256:' + 'a' * 64}}}, 'binaries': {}}
            (work / 'release.json').write_text(json.dumps(manifest))
            source = (ROOT / 'infra/scripts/30-frontend.sh').read_text()
            section = source.split('# 1. Pull panel image', 1)[1].split('# 2. Generate', 1)[0]
            section = section.split('\n', 2)[2]
            section = section.replace('$(dirname "$0")/release.sh', shlex.quote(str(ROOT / 'infra/lib/release.sh')))
            run = subprocess.run(['bash', '-euo', 'pipefail', '-c', 'HOST_TAG=ru\n' + section],
                                 env=dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'],
                                          VPN2_RELEASE_MANIFEST=str(work / 'release.json'),
                                          VPN2_DEVELOPMENT='false', PANEL_IMAGE='example/panel:1.0.1', PANEL_IMAGE_PULL='false'),
                                 capture_output=True, text=True, timeout=15)
            self.assertNotEqual(run.returncode, 0, 'deploy accepted retagged foreign image')
            self.assertIn('digest', run.stderr)
            print('retagged image: rejected before panel changes')

    def test_matching_offline_image_uses_immutable_id(self):
        release = module('release')
        expected = 'sha256:' + 'a' * 64
        manifest = {'images': {'panel': {'amd64': {'repository': 'example/panel', 'digest': expected, 'config': expected}}}}
        with patch.object(release, 'command', return_value=json.dumps([{'Id': expected, 'Architecture': 'amd64'}])) as run:
            self.assertEqual(release.image(manifest, 'panel', 'example/panel:1.0.1', False, 'amd64'), expected)
        self.assertEqual(run.call_count, 1)

    def test_floating_tags_require_development(self):
        release = module('release')
        for tag in ('dev', 'main', 'latest'):
            with self.assertRaisesRegex(release.ReleaseError, 'development'):
                release.image({}, 'panel', 'example/panel:' + tag)

    def test_registry_timeout_still_requires_matching_local_digest(self):
        release = module('release')
        expected = 'sha256:' + 'a' * 64
        manifest = {'images': {'panel': {'amd64': {'repository': 'example/panel', 'digest': expected, 'config': expected}}}}
        answers = [subprocess.TimeoutExpired(['docker', 'pull'], 300),
                   json.dumps([{'Id': expected, 'Architecture': 'amd64'}])]
        with patch.object(release, 'command', side_effect=answers):
            self.assertEqual(release.image(manifest, 'panel', arch='amd64'), expected)

    def test_failed_compatibility_never_starts_previous_container(self):
        library = ROOT / 'infra/lib/panel-transaction.sh'
        self.assertTrue(library.exists(), 'deploy has no rollback schema guard')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / 'previous-schema.json').write_text('{}')
            marker = work / 'docker-called'
            run = subprocess.run(['bash', '-euo', 'pipefail', '-c', f'''
source {shlex.quote(str(library))}
panel_state={shlex.quote(str(work))}
panel_compatible() {{ return 1; }}
docker() {{ touch {shlex.quote(str(marker))}; }}
panel_rollback
'''], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertIn('incompatible schema', run.stderr)

    def test_corrupt_download_rejected_before_copy_or_extract(self):
        release = module('release')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            expected = hashlib.sha256(b'original archive').hexdigest()
            manifest = {'binaries': {'binary': {'url': 'https://example.invalid/archive', 'sha256': expected}}}
            (work / expected).write_bytes(b'corrupted archive')
            destination = work / 'install.tar'
            with patch('urllib.request.urlopen', side_effect=lambda *_a, **_k: io.BytesIO(b'corrupted')) as fetch:
                with self.assertRaisesRegex(release.ReleaseError, 'before extraction'):
                    release.artifact(manifest, 'binary', work, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(fetch.call_count, 1)
            print('corrupt artifact: rejected before extraction; destination unchanged')

    def test_corrupt_cache_is_downloaded_again_once(self):
        release = module('release')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            expected = hashlib.sha256(b'original archive').hexdigest()
            manifest = {'binaries': {'binary': {'url': 'https://example.invalid/archive', 'sha256': expected}}}
            cached = work / expected
            cached.write_bytes(b'original archivX')
            destination = work / 'install.tar'
            with patch('urllib.request.urlopen',
                       side_effect=lambda *_a, **_k: io.BytesIO(b'original archive')) as fetch:
                release.artifact(manifest, 'binary', work, destination)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(destination.read_bytes(), b'original archive')
            self.assertEqual(cached.read_bytes(), b'original archive')
            destination.unlink()
            cached.write_bytes(b'original archivX')
            with patch('urllib.request.urlopen', side_effect=lambda *_a, **_k: io.BytesIO(b'mirror broken')) as fetch:
                with self.assertRaises(release.ReleaseError) as caught:
                    release.artifact(manifest, 'binary', work, destination)
            self.assertEqual(fetch.call_count, 1)
            self.assertIn(str(cached), str(caught.exception))
            self.assertFalse(destination.exists())
            self.assertEqual(sorted(path.name for path in work.iterdir()), [])
            print(caught.exception)

    def test_wal_backup_contains_committed_wal(self):
        state = module('deploy_state')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            database = work / 'live.sqlite'
            with closing(sqlite3.connect(database)) as live, live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('PRAGMA wal_autocheckpoint=0')
                live.execute('CREATE TABLE access(id INTEGER, allowed INTEGER)')
                live.execute('INSERT INTO access VALUES(1, 0)')
                live.commit()
                self.assertGreater(Path(str(database) + '-wal').stat().st_size, 0)
                state.backup(database, work / 'snapshot', [])
                with closing(sqlite3.connect(work / 'snapshot/db.sqlite')) as copy, copy:
                    self.assertEqual(copy.execute('SELECT * FROM access').fetchall(), [(1, 0)])
                state.verify_backup(work / 'snapshot')
            print('SQLite backup: committed WAL included; integrity_check=ok')

    def journal_runner(self, state, work):
        """deploy_state.main() against a temporary state; host config paths are not snapshotted."""
        release = module('release')
        os_values = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        manifest = {'format': 1, 'commit': 'a' * 40, 'rollback': 'backward-compatible', 'images': {},
                    'binaries': {}, 'architectures': [release.architecture()],
                    'os': {os_values['ID'].strip('"'): [os_values.get('VERSION_ID', '').strip('"')]},
                    'min_free_bytes': 0, 'min_memory_bytes': 0, 'local_service_api': {'min': 0, 'max': 999},
                    'database': {'min': 0, 'max': 0, 'migrate_from': [0, 0], 'journal': []},
                    'stages': ['10-foreign', '99-verify']}
        (work / 'manifest.json').write_text(json.dumps(manifest))
        original_backup = state.backup

        def run(folder, *arguments):
            argv = ['deploy_state.py', '--state', str(folder), '--database', str(work / 'db.sqlite'),
                    '--manifest', str(work / 'manifest.json'), *arguments]
            with patch.object(sys, 'argv', argv), \
                    patch.object(state, 'backup', lambda db, destination, _paths: original_backup(db, destination, [])):
                self.assertEqual(state.main(), 0, arguments)
        return run

    def test_successful_deploy_and_panel_transaction_rotate_backups(self):
        state = module('deploy_state')
        keep = getattr(state, 'KEEP_BACKUPS', 3)
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            with closing(sqlite3.connect(work / 'db.sqlite')) as db, db:
                db.execute('CREATE TABLE access(id INTEGER)')
            run = self.journal_runner(state, work)
            deploy, panel = work / 'deploy', work / 'deploy/panel'
            # Leftover of a backup that failed half-way: never marked complete.
            (deploy / 'backups/1').mkdir(parents=True)
            for _ in range(keep + 2):
                run(deploy, 'backup')
                newest = json.loads((deploy / 'operation.json').read_text())['backup']
                run(deploy, 'phase', '--phase', 'applying')
                run(deploy, 'complete', '--role', 'exit')
            left = sorted(p.name for p in (deploy / 'backups').iterdir())
            print(f'deploy: {keep + 2} successful deploys -> {len(left)} backups kept')
            self.assertEqual(len(left), keep, f'deploy backups are not rotated: {left}')
            self.assertIn(Path(newest).name, left, 'rotation removed the newest deploy backup')
            # Panel transaction: two snapshots each (before and inside the maintenance window).
            (panel / 'previous-schema.json').parent.mkdir(parents=True, exist_ok=True)
            (panel / 'previous-schema.json').write_text('{}')
            (panel / 'previous-installed.json').write_text('{"release": null}')
            for _ in range(keep + 2):
                run(panel, 'backup')
                run(panel, 'backup')
                newest = json.loads((panel / 'operation.json').read_text())['backup']
                run(panel, 'phase', '--phase', 'complete')
            left = sorted(p.name for p in (panel / 'backups').iterdir())
            print(f'panel: {keep + 2} transactions -> {len(left)} backups kept')
            self.assertEqual(len(left), keep, f'panel backups are not rotated: {left}')
            self.assertIn(Path(newest).name, left, 'rotation removed the newest panel backup')
            for name in left:
                state.verify_backup(panel / 'backups' / name)
            self.assertEqual(len(list((deploy / 'backups').iterdir())), keep, 'panel rotation touched deploy backups')
            # Rollback after rotation: its inputs live outside backups/ and still work.
            library = ROOT / 'infra/lib/panel-transaction.sh'
            script = f'''source {shlex.quote(str(library))}
panel_state={shlex.quote(str(panel))}
panel_compatible() {{ test -f "$1"; }}
panel_health() {{ return 0; }}
python3() {{ return 0; }}
docker() {{ :; }}
panel_rollback
'''
            rollback = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, timeout=10)
            self.assertEqual(rollback.returncode, 0, rollback.stderr)
            self.assertIn('panel rollback verified', rollback.stdout)
            print('rollback after rotation: ' + rollback.stdout.strip())

    def test_disk_space_refusal_names_backup_folders(self):
        state = module('deploy_state')
        release = module('release')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            for folder, count in (('backups', 2), ('panel/backups', 3)):
                for index in range(count):
                    item = work / folder / str(1000 + index)
                    item.mkdir(parents=True)
                    (item / 'db.sqlite').write_bytes(b'x' * 1024 * 1024)
            (work / 'os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
            (work / 'meminfo').write_text('MemAvailable: 4000000 kB\n')
            manifest = {'os': {'ubuntu': ['26.04']}, 'architectures': [release.architecture()],
                        'min_free_bytes': 2 ** 62, 'min_memory_bytes': 0}
            with self.assertRaises(state.ReleaseError) as refused:
                state.preflight(manifest, work / 'db.sqlite', work, work / 'os-release', work / 'meminfo')
            message = str(refused.exception)
            print('deploy stopped: ' + message)
            self.assertIn('services unchanged', message)
            self.assertIn(f'{work / "backups"} 2 MiB in 2', message, 'refusal does not say where deploy backups are')
            self.assertIn(f'{work / "panel/backups"} 3 MiB in 3', message, 'refusal does not say where panel backups are')

    def test_exit_release_journal_never_starts_docker(self):
        # Stage 23 stops Docker on exits; docker.socket restarts dockerd on any
        # client call, so every journal action outside stage 23 must avoid it.
        state = module('deploy_state')
        release = module('release')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            tools = work / 'bin'
            tools.mkdir()
            calls = work / 'docker-calls'
            fake = tools / 'docker'
            fake.write_text(f'#!/bin/sh\necho "$*" >> {shlex.quote(str(calls))}\necho sha256:{"c" * 64}\n')
            fake.chmod(0o755)
            os_values = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
                             if '=' in line)
            manifest = {'format': 1, 'commit': 'a' * 40, 'rollback': 'backward-compatible', 'images': {},
                        'binaries': {}, 'architectures': [release.architecture()],
                        'os': {os_values['ID'].strip('"'): [os_values.get('VERSION_ID', '').strip('"')]},
                        'min_free_bytes': 0, 'min_memory_bytes': 0, 'local_service_api': {'min': 0, 'max': 999},
                        'database': {'min': 0, 'max': 0, 'migrate_from': [0, 0], 'journal': []},
                        'stages': ['10-foreign', '23-emergency', '99-verify']}
            (work / 'manifest.json').write_text(json.dumps(manifest))
            original_backup = state.backup

            def run(*arguments):
                argv = ['deploy_state.py', '--state', str(work / 'state'), '--database', str(work / 'db.sqlite'),
                        '--manifest', str(work / 'manifest.json'), *arguments]
                # Configuration paths are the host's own; the snapshot logic is the same without them.
                with patch.object(sys, 'argv', argv), \
                        patch.dict(os.environ, PATH=f'{tools}:' + os.environ['PATH']), \
                        patch.object(state, 'backup', lambda db, destination, _paths: original_backup(db, destination, [])):
                    self.assertEqual(state.main(), 0, arguments)

            for arguments in (['preflight'], ['prepare', '--role', 'exit'], ['backup'],
                              ['phase', '--phase', 'applying'], ['complete', '--role', 'exit'],
                              ['preflight', '--partial'], ['prepare', '--role', 'exit', '--partial']):
                run(*arguments)
            exit_calls = calls.read_text().splitlines() if calls.exists() else []
            print(f'exit journal docker calls: {len(exit_calls)} {exit_calls}')
            self.assertEqual(exit_calls, [], 'release journal starts docker on an exit after stage 23')
            # Positive control: the entry records its panel containers through the same fake.
            run('complete', '--role', 'entry')
            self.assertIn('inspect --format {{.Image}} anysda-vpn2', calls.read_text())
            installed = json.loads((work / 'state/installed.json').read_text())
            self.assertEqual(installed['actual']['images']['anysda-vpn2'], 'sha256:' + 'c' * 64)

    def test_failed_backup_does_not_stop_panel_or_change_database(self):
        library = ROOT / 'infra/lib/panel-transaction.sh'
        self.assertTrue(library.exists(), 'deploy has no mandatory backup transaction')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            marker = work / 'mutated'
            script = f'''source {shlex.quote(str(library))}
panel_state={shlex.quote(str(work / 'state'))}
PANEL_IMAGE=test
python3() {{ [[ " $* " != *' backup '* && " $* " != *' backup' ]]; }}
docker() {{ touch {shlex.quote(str(marker))}; }}
panel_begin
'''
            run = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse(marker.exists(), 'working container changed despite failed backup')
            print('backup failure: no stop, migration or working-data change')

    def test_incompatible_schema_prevents_old_container_start(self):
        state = module('deploy_state')
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'db.sqlite'
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE __drizzle_migrations (hash TEXT, created_at INTEGER)')
                db.execute("INSERT INTO __drizzle_migrations VALUES ('new', 2)")
            contract = {'journal': [{'sha256': 'old', 'when': 1}], 'min': 1, 'max': 1}
            with self.assertRaisesRegex(state.ReleaseError, 'old image must not start'):
                state.compatible(database, contract)
            print('incompatible schema: old image must not start')

    def test_rollback_retains_current_access_and_pki(self):
        library = ROOT / 'infra/lib/panel-transaction.sh'
        self.assertTrue(library.exists(), 'deploy has no safe rollback')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            database = work / 'db.sqlite'
            with closing(sqlite3.connect(database)) as db, db:
                db.executescript('CREATE TABLE devices(id INTEGER, allowed INTEGER, expires_at INTEGER);'
                                 'INSERT INTO devices VALUES (1,0,1); INSERT INTO devices VALUES (2,1,9999999999);'
                                 'CREATE TABLE operations(id INTEGER, status TEXT); INSERT INTO operations VALUES(1,"pending");')
            crl = work / 'crl.pem'
            crl.write_bytes(b'current revocation list')
            before = (database.read_bytes(), crl.read_bytes())
            (work / 'previous-schema.json').write_text('{}')
            script = f'''source {shlex.quote(str(library))}
panel_state={shlex.quote(str(work))}
panel_compatible() {{ return 0; }}
panel_health() {{ return 0; }}
python3() {{ return 0; }}
docker() {{ printf '%s\\n' "$*"; }}
panel_rollback
'''
            run = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((database.read_bytes(), crl.read_bytes()), before)
            self.assertIn('current database, revocations, expirations, operations and PKI retained', run.stdout)
            print('rollback: revoked device denied, expiry and pending operations retained; other client allowed')

    def build_manifest(self, work, migrations_after_tag):
        """Release checkout: tag v1.0.0, then commits; returns the generated manifest."""
        repo = work / 'repo'
        folder = repo / 'app/server/database/migrations'
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
        subprocess.run(git[:3] + ['add', '-A'], check=True)
        subprocess.run(git + ['commit', '-qm', 'later'], check=True)
        builder = module('build-release')
        output = work / 'release-manifest.json'
        argv = ['build-release.py', '--root', str(repo), '--digests', str(work), '--output', str(output)]
        with patch.object(builder, 'resolve_image', return_value={}), \
                patch.object(builder, 'binary_specs', return_value={}), \
                patch.object(builder, 'source_files', return_value={}), \
                patch.object(sys, 'argv', argv):
            for name in ('panel', 'tgbot', 'emergency'):
                (work / (name + '.txt')).write_text('sha256:' + 'a' * 64)
            builder.main()
        return json.loads(output.read_text()), output

    def test_release_with_new_migration_does_not_promise_rollback(self):
        release = module('release')
        with tempfile.TemporaryDirectory() as directory:
            manifest, path = self.build_manifest(Path(directory), migrations_after_tag=1)
            self.assertEqual(manifest['rollback'], 'forward-only',
                             'manifest promises rollback to the previous release across a new migration')
            self.assertEqual(release.load(path)['rollback'], 'forward-only')
        with tempfile.TemporaryDirectory() as directory:
            manifest, path = self.build_manifest(Path(directory), migrations_after_tag=0)
            self.assertEqual(manifest['rollback'], 'backward-compatible')
            release.load(path)
        print('manifest: new migration -> forward-only; same schema -> backward-compatible')

    def test_journal_closes_sqlite_connections(self):
        # An unclosed connection warns at a random garbage collection and lands in
        # captured deploy output (preflight warning checks above).
        state = module('deploy_state')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            with closing(sqlite3.connect(work / 'db.sqlite')) as db, db:
                db.execute('CREATE TABLE __drizzle_migrations (hash TEXT, created_at INTEGER)')
            gc.collect()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always', ResourceWarning)
                state.schema(work / 'db.sqlite')
                state.backup(work / 'db.sqlite', work / 'backup', [])
                gc.collect()
            self.assertEqual([str(w.message) for w in caught if w.category is ResourceWarning], [])
        print('deploy journal: sqlite connections closed after schema, backup and verification')

    def test_deploy_warns_before_migration_that_closes_rollback(self):
        state = module('deploy_state')
        release = module('release')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            database = work / 'db.sqlite'
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE __drizzle_migrations (hash TEXT, created_at INTEGER)')
                db.execute("INSERT INTO __drizzle_migrations VALUES ('a', 1)")
            (work / 'os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
            (work / 'meminfo').write_text('MemAvailable: 4000000 kB\n')
            journal = [{'when': 1, 'sha256': 'a'}, {'when': 2, 'sha256': 'b'}]
            manifest = {'os': {'ubuntu': ['26.04']}, 'architectures': [release.architecture()],
                        'min_free_bytes': 0, 'min_memory_bytes': 0, 'local_service_api': {'min': 1, 'max': 1},
                        'rollback': 'forward-only',
                        'database': {'min': 2, 'max': 2, 'migrate_from': [0, 2], 'journal': journal}}

            def warning(contract_journal, **options):
                manifest['database']['journal'] = contract_journal
                count = len(contract_journal)
                manifest['database'].update(min=count, max=count, migrate_from=[0, count])
                with patch('sys.stderr', new_callable=lambda: __import__('io').StringIO()) as err:
                    state.preflight(manifest, database, work, work / 'os-release', work / 'meminfo', **options)
                return err.getvalue()

            text = warning(journal)
            self.assertIn('rollback', text, 'deploy applies a migration that closes rollback without warning')
            self.assertIn('1 -> 2', text)
            self.assertEqual(warning(journal[:1]), '', 'same schema must keep rollback without warning')
            self.assertEqual(warning(journal, warn=False), '', 'prepare repeats the preflight warning')
        print('deploy preflight: migration 1 -> 2 warns before apply; same schema silent')

    def test_migration_is_explicit_idempotent_and_rejects_future_history(self):
        migration = ROOT / 'app/scripts/migrate.mjs'
        self.assertTrue(migration.exists(), 'no explicit deploy migration executor')
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'db.sqlite'
            env = dict(os.environ, NUXT_DATABASE_URL='file:' + str(database))
            for _ in range(2):
                run = subprocess.run(['node', str(migration)], cwd=ROOT / 'app', env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stderr)
            with closing(sqlite3.connect(database)) as db, db:
                count = db.execute('SELECT count(*) FROM __drizzle_migrations').fetchone()[0]
                db.execute("INSERT INTO __drizzle_migrations(hash,created_at) VALUES ('future',9999999999999)")
            run = subprocess.run(['node', str(migration)], cwd=ROOT / 'app', env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(run.returncode, 0)
            with closing(sqlite3.connect(database)) as db, db:
                self.assertEqual(db.execute('SELECT count(*) FROM __drizzle_migrations').fetchone()[0], count + 1)
            print('migration executor: repeat is idempotent; future history rejected without writes')


if __name__ == '__main__':
    unittest.main(verbosity=2)
