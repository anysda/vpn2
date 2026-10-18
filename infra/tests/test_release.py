"""Release acceptance checks. VPN2_TEST_ROOT selects the unchanged baseline."""
import hashlib
import importlib.util
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
            with self.assertRaisesRegex(release.ReleaseError, 'before extraction'):
                release.artifact(manifest, 'binary', work, destination)
            self.assertFalse(destination.exists())
            print('corrupt artifact: rejected before extraction; destination unchanged')

    def test_wal_backup_contains_committed_wal(self):
        state = module('deploy_state')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            database = work / 'live.sqlite'
            with sqlite3.connect(database) as live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('PRAGMA wal_autocheckpoint=0')
                live.execute('CREATE TABLE access(id INTEGER, allowed INTEGER)')
                live.execute('INSERT INTO access VALUES(1, 0)')
                live.commit()
                self.assertGreater(Path(str(database) + '-wal').stat().st_size, 0)
                state.backup(database, work / 'snapshot', [])
                with sqlite3.connect(work / 'snapshot/db.sqlite') as copy:
                    self.assertEqual(copy.execute('SELECT * FROM access').fetchall(), [(1, 0)])
                state.verify_backup(work / 'snapshot')
            print('SQLite backup: committed WAL included; integrity_check=ok')

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
            with sqlite3.connect(database) as db:
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
            with sqlite3.connect(database) as db:
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
            for name in ('panel', 'tgbot'):
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

    def test_deploy_warns_before_migration_that_closes_rollback(self):
        state = module('deploy_state')
        release = module('release')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            database = work / 'db.sqlite'
            with sqlite3.connect(database) as db:
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
            with sqlite3.connect(database) as db:
                count = db.execute('SELECT count(*) FROM __drizzle_migrations').fetchone()[0]
                db.execute("INSERT INTO __drizzle_migrations(hash,created_at) VALUES ('future',9999999999999)")
            run = subprocess.run(['node', str(migration)], cwd=ROOT / 'app', env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(run.returncode, 0)
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM __drizzle_migrations').fetchone()[0], count + 1)
            print('migration executor: repeat is idempotent; future history rejected without writes')


if __name__ == '__main__':
    unittest.main(verbosity=2)
