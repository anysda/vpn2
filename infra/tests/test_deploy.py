"""Регрессии развёртывания: реальные фрагменты стадий с подставными командами.

VPN2_TEST_ROOT позволяет прогнать те же проверки против другой ревизии.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


def source(path):
    return (ROOT / path).read_text()


def function(name):
    body = source('infra/deploy.sh').split(f'{name}() {{', 1)[1].split('\n}', 1)[0]
    return f'{name}() {{' + body + '\n}\n'


def bash(script, directory):
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                          cwd=directory, text=True, capture_output=True, timeout=15)


class DeployTests(unittest.TestCase):
    def test_restore_stages_preserve_archive_password_but_deploy_applies_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            for stage in ('29-openvpn', '20-ru-router', '21-failover-watchdog',
                          '26-backup', '27-ikev2', '35-telegram', '30-frontend'):
                (root / 'scripts' / (stage + '.sh')).touch()
            (root / 'envs').mkdir()
            (root / 'envs/all.env').write_text('HOST_TAG=ru\n')
            (root / 'envs/ru.env').write_text('ADMIN_PASSWORD=current-seed\n')
            (root / 'secrets').mkdir()
            (root / 'secrets/foreign-secrets.env').touch()
            (root / 'lib').mkdir()
            for library in ('gen-router-config.py', 'failover-watchdog.py', 'anysda-backup.sh',
                            'anysda-restore.sh', 'anysda-backup-list.sh'):
                (root / 'lib' / library).touch()
            script = function('run_stage_on_host') + function('do_restore') + '''
DEPLOY_ROOT=$PWD
load_env() { :; }
log() { :; }
ok() { :; }
ssh_exec() { :; }
push() {
  if [[ $1 == */rendered/ru.env ]]; then
    ( source "$1"; echo "password=${ADMIN_PASSWORD:-archive}" )
  fi
}
run_stage() { run_stage_on_host "$1" "$2"; }
do_restore latest --force
run_stage 30-frontend ru
'''
            result = bash(script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['password=archive'] * 7 + ['password=current-seed'])

    def test_restore_dry_run_only_asks_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            script = function('do_restore') + '''
load_env() { :; }
ssh_exec() { echo "ssh: $1"; }
run_stage() { echo "stage: $1"; }
do_restore latest --force --dry-run
'''
            result = bash(script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(),
                             ["ssh: /usr/local/bin/anysda-restore.sh 'latest' '--force' '--dry-run'"])

    def test_failed_upload_never_runs_stale_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'scripts/30-frontend.sh').touch()
            (root / 'envs').mkdir()
            (root / 'envs/all.env').write_text('COMMON=1\n')
            (root / 'envs/ru.env').write_text('HOST_TAG=ru\nSSH_PASS=fake\n')
            script = function('run_stage_on_host') + '''
DEPLOY_ROOT=$PWD
load_env() { return 0; }
log() { :; }
ok() { :; }
warn() { :; }
die() { exit 1; }
count=0
push() { count=$((count + 1)); [[ $count != $FAIL_AT ]]; }
ssh_exec() {
  if [[ $* == *apt_prepare* ]]; then [[ $FAIL_AT != apt ]]; return; fi
  echo "REMOTE EXECUTED"
}
if run_stage_on_host 30-frontend ru; then exit 0; else exit 23; fi
'''
            for failure in ('1', '2', '3', '4', 'apt'):
                with self.subTest(failure=failure):
                    result = bash(f'FAIL_AT={failure}\n' + script, directory)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertNotIn('REMOTE EXECUTED', result.stdout)
            result = bash('FAIL_AT=none\n' + script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count('REMOTE EXECUTED'), 1)


if __name__ == '__main__':
    unittest.main()
