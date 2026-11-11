"""Регрессии развёртывания: реальные фрагменты стадий с подставными командами.

VPN2_TEST_ROOT позволяет прогнать те же проверки против другой ревизии.
"""
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

from play_stage import PLAY_STUBS, play_tree, run_play_source


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
        stubs = '''
DEPLOY_ROOT=$PWD
load_env() { export ADMIN_PASSWORD=current-seed; }
log() { :; }
ok() { :; }
ssh_exec() { :; }
push() { :; }
fw_guard_stage() { return 1; }
'''
        with tempfile.TemporaryDirectory() as directory:
            play_tree(directory, ('29-openvpn', '20-ru-router', '21-failover-watchdog', '26-backup',
                                  '27-ikev2', '35-telegram', '30-frontend'), stubs,
                      'echo "password=${ADMIN_PASSWORD:-archive} preserve=${VPN2_PRESERVE_ADMIN:-0}"\n')
            script = (function('run_stage_on_host') + function('do_restore') + run_play_source()
                      + stubs + PLAY_STUBS + '''
run_stage() { run_stage_on_host "$1" "$2"; }
do_restore latest --force
run_stage 30-frontend ru
''')
            result = bash(script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(),
                             ['[ru]password=archive preserve=1'] * 7 + ['[ru]password=current-seed preserve=0'])

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
            lines = result.stdout.splitlines()
            self.assertEqual(len(lines), 1, lines)
            self.assertRegex(lines[0], r"^ssh: VPN2_DEVELOPMENT=(true|false) "
                                       r"/usr/local/bin/anysda-restore\.sh 'latest' '--force' '--dry-run'$")

    def run_deploy(self, directory, *args, restore_rc=0, backup_rc=0):
        """Настоящие infra/deploy.sh и deploy-release.sh, транспорт до entry подставной."""
        root = Path(directory)
        (root / 'lib').mkdir()
        (root / 'bin').mkdir()
        for path in ('infra/deploy.sh', 'infra/lib/deploy-release.sh'):
            (root / path.removeprefix('infra/')).write_text(source(path))
        (root / 'lib/anysda-svc.sh').write_text('')
        (root / 'lib/ansible-venv.sh').write_text('apt_have() { return 0; }\nansible_venv_start() { :; }\n')
        (root / 'scripts').mkdir()
        (root / 'scripts/30-frontend.sh').touch()
        for tool in ('ssh', 'scp', 'sshpass', 'envsubst', 'curl', 'rsync', 'openssl'):
            (root / 'bin' / tool).write_text('#!/bin/sh\nexit 0\n')
            (root / 'bin' / tool).chmod(0o755)
        (root / 'lib/ssh.sh').write_text(f'''
C_R= C_G= C_Y= C_B= C_END=
log() {{ :; }}; ok() {{ :; }}; warn() {{ echo "warn: $*" >&2; }}
die() {{ echo "die: $*" >&2; exit 1; }}
load_env() {{ HOST_TAG=$1; }}
expand_hosts() {{ case $1 in ru|all) echo ru ;; *) die "unknown host group: $1" ;; esac; }}
push() {{ echo "push: $*" >> "$DEPLOY_ROOT/calls"; }}
ssh_exec() {{
  echo "ssh: $1" >> "$DEPLOY_ROOT/calls"
  if [[ "$1" == *'exec flock'* ]]; then echo locked; read -r unlock; fi
  # Пробный проход до выпуска архив находит, падает само восстановление.
  if [[ "$1" == *anysda-restore.sh*--dry-run* ]]; then return 0; fi
  if [[ "$1" == *anysda-restore.sh* ]]; then return {restore_rc}; fi
  if [[ "$1" == *anysda-backup.sh* ]]; then
    # Код 4 настоящей копии при расхождении - со строкой warning: в финале.
    if [[ {backup_rc} -eq 4 ]]; then
      printf '%-12s %s\\n' warning: 'БД и конфиги VPN расходятся (устройства: 7) — перевыпусти в панели'
    fi
    return {backup_rc}
  fi
}}
''')
        environment = dict(os.environ, PATH=f'{root / "bin"}:{os.environ["PATH"]}',
                           VPN2_DEVELOPMENT='true')
        result = subprocess.run(['bash', str(root / 'deploy.sh'), *args], cwd=directory, env=environment,
                                text=True, capture_output=True, timeout=15)
        calls = root / 'calls'
        return result, calls.read_text().splitlines() if calls.exists() else []

    def test_restore_dry_run_opens_no_release(self):
        for args, archive in ((('restore', 'latest', '--dry-run'), 'latest'),
                              (('restore', '--dry-run'), 'latest'),
                              (('restore', '--dry-run', '--force'), 'latest'),
                              (('restore', 'anysda-vpn2-x.tar.gz.age', '--force', '--dry-run'),
                               'anysda-vpn2-x.tar.gz.age')):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                result, calls = self.run_deploy(directory, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(calls), 1, calls)
                self.assertRegex(calls[0], r'^ssh: VPN2_DEVELOPMENT=(true|false) '
                                           r'/usr/local/bin/anysda-restore\.sh ' + re.escape(f"'{archive}'"))
                self.assertIn("'--dry-run'", calls[0])

    def test_restore_dry_run_of_local_archive_uploads_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'anysda-vpn2-2026-10-18T000000Z.tar.gz.age'
            archive.write_bytes(b'archive')
            result, calls = self.run_deploy(directory, 'restore', str(archive), '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls, [])
            self.assertIn(archive.name, result.stdout + result.stderr)

    def test_backup_saved_with_warning_is_not_a_failure(self):
        # Код 4 скрипта бэкапа: архив снят, но БД и конфиги VPN расходятся.
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.run_deploy(directory, 'backup', backup_rc=4)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls, ['ssh: /usr/local/bin/anysda-backup.sh'])
            self.assertIn('warn: архив сохранён с предупреждением', result.stderr)
            self.assertIn('перевыпусти', result.stderr)

    def test_failed_backup_still_fails(self):
        for rc in (1, 2, 3):
            with self.subTest(rc=rc), tempfile.TemporaryDirectory() as directory:
                result, _ = self.run_deploy(directory, 'backup', backup_rc=rc)
                self.assertEqual(result.returncode, rc, result.stderr)
                self.assertNotIn('архив сохранён', result.stderr)

    def test_invalid_stage_arguments_open_no_release(self):
        # Иначе выпуск остаётся в applying, и следующий деплой требует recover.
        for args in (('30-frontend',), ('30-frontnd', 'ru'), ('30-frontend', 'nosuch')):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                result, calls = self.run_deploy(directory, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_restore_still_opens_release_before_changing_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.run_deploy(directory, 'restore', 'latest', '--force', restore_rc=1)
            self.assertNotEqual(result.returncode, 0)
            remote = [call for call in calls if call.startswith('ssh: ')]
            restore = next(i for i, call in enumerate(remote)
                           if 'anysda-restore.sh' in call and '--dry-run' not in call)
            before = '\n'.join(remote[:restore])
            for step in ('exec flock', 'deploy_state.py backup', 'deploy_state.py phase --phase applying'):
                self.assertIn(step, before)

    def test_failed_upload_never_runs_stale_play(self):
        stubs = '''
DEPLOY_ROOT=$PWD
FW_GUARD_WINDOW=90
load_env() { HOST_TAG=$1; }
log() { :; }
ok() { :; }
warn() { :; }
die() { exit 1; }
count=0
push() { count=$((count + 1)); [[ $count != $FAIL_AT ]]; }
ssh_exec() {
  if [[ $* == *apt_prepare* ]]; then [[ $FAIL_AT != apt ]]; return; fi
  if [[ $* == *'fw-guard.sh arm'* ]]; then [[ $FAIL_AT != arm ]]; return; fi
}
fw_guard_stage() { return 0; }
fw_guard_entry_baseline() { :; }
fw_guard_beat() { command sleep 5; }
fw_guard_disarm() { :; }
'''
        with tempfile.TemporaryDirectory() as directory:
            play_tree(directory, ['30-frontend'], stubs, 'echo "PLAY EXECUTED"\n')
            script = (function('run_stage_on_host') + run_play_source() + stubs + PLAY_STUBS
                      + 'if run_stage_on_host 30-frontend ru; then exit 0; else exit 23; fi\n')
            # Библиотека стадий, сторож фаервола, подготовка apt, взвод сторожа.
            for failure in ('1', '2', 'apt', 'arm'):
                with self.subTest(failure=failure):
                    result = bash(f'FAIL_AT={failure}\n' + script, directory)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertNotIn('PLAY EXECUTED', result.stdout)
            result = bash('FAIL_AT=none\n' + script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count('PLAY EXECUTED'), 1)


if __name__ == '__main__':
    unittest.main()
