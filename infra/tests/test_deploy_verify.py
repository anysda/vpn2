"""Real orchestration and verify, with SSH/host commands replaced by fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT, source
import test_verify


def deploy_source():
    ref = os.environ.get('VERIFY_DEPLOY_REF')
    return subprocess.check_output(['git', 'show', f'{ref}:infra/deploy.sh'],
                                   cwd=ROOT, text=True) if ref else source('infra/deploy.sh')


def orchestrate(ru, foreign, mode='full', failure=''):
    with tempfile.TemporaryDirectory(prefix='deploy-verify-') as directory:
        root = Path(directory)
        for subdir in ('envs', 'scripts', 'lib', 'configs', 'secrets', 'tmp', 'emergency'):
            (root / subdir).mkdir()
        (root / 'envs/all.env').write_text('ENTRY_HOST=entry.example\nADMIN_PASSWORD=fixture\n')
        for host, result in (('ru', ru), ('exit', foreign)):
            (root / f'envs/{host}.env').write_text(
                f'HOST_TAG={host}\nTELEGRAM_BOT_TOKEN=fixture\nTELEGRAM_CHAT_ID=1\n')
            (root / f'{host}.out').write_text(result.stdout)
            (root / f'{host}.rc').write_text(str(result.returncode))
        for path in (ROOT / 'infra/scripts').glob('*.sh'):
            (root / 'scripts' / path.name).touch()
        for path in ('lib/gen-router-config.py', 'lib/failover-watchdog.py',
                     'lib/anysda-svc.sh', 'emergency/provision.py',
                     'lib/anysda-backup.sh', 'lib/anysda-restore.sh', 'lib/anysda-backup-list.sh',
                     'configs/sing-box-server.json.tpl', 'secrets/foreign-secrets.env'):
            (root / path).touch()
        script = 'set -euo pipefail\nDEPLOY_ROOT=$PWD\n'
        # All functions, globals and dispatch are real; skip sourcing SSH helpers
        # (which creates persistent local SSH state) and supply fixture I/O below.
        deploy = deploy_source()
        script += deploy[deploy.index('_STAGE_LOG='):deploy.index('# Без аргументов → полный pipeline.')]
        helpers = source('infra/lib/ssh.sh')
        script += helpers[helpers.index('log()'):helpers.index('# Load env')]
        script += r'''
C_R= C_G= C_Y= C_B= C_END=
date() { echo 100; }
load_env() { HOST_TAG=$1; }
expand_hosts() { if [[ $1 == all ]]; then echo 'ru exit'; else echo "$1"; fi; }
push() { [[ $FAILURE != push ]] || return 2; }
ssh_exec() {
  if [[ $* == *apt_prepare* ]]; then [[ $FAILURE != apt ]] || return 2
  elif [[ $* == *99-verify.sh* ]]; then
    [[ $HOST_TAG != ru ]] || sleep 1  # экзит завершается раньше ru: ловит потерю кода в wait
    cat "$DEPLOY_ROOT/$HOST_TAG.out"
    return "$(<"$DEPLOY_ROOT/$HOST_TAG.rc")"
  elif [[ $* == *deploy_done* ]]; then
    printf '%s\n' "$*" > "$DEPLOY_ROOT/event"
  elif [[ $* == *30-frontend.sh* && $FAILURE == other-stage ]]; then
    return 2
  fi
}
print_banner() { :; }
pause_entry_auto_updates() { :; }
install_prereqs() { :; }
do_check() { :; }
preflight_ssh() { :; }
prep_orchestrator_key() { :; }
prep_entry_mgmt_key() { :; }
prep_ru_router() { :; }
verify_and_rotate_ports() { :; }
run_tracks() { :; }
release_complete() { :; }
prefetch_images() { true & _PREFETCH_PID=$!; }
print_ikev2_notice() { :; }
print_sso_notice() { :; }
'''
        script += 'do_all\n' if mode == 'full' else 'run_stage 99-verify all\n'
        env = {**os.environ, 'FAILURE': failure, 'TMPDIR': str(root / 'tmp')}
        env.pop('BASH_ENV', None)
        result = subprocess.run(['bash', '-c', script], cwd=root, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
        event = root / 'event'
        result.event = event.read_text() if event.exists() else ''
        if list((root / 'tmp').iterdir()):
            raise AssertionError('orchestrator left temporary logs behind')
        return result


class DeployVerifyTests(unittest.TestCase):
    def test_real_verify_through_full_and_single_stage(self):
        verifier = test_verify.VerifyTest()
        foreign = verifier.run_verify(role='exit')
        # 2 — «не проверено»: установка прошла, но код доходит до CI.
        cases = [('', 0), ('yt-missing-state', 2), ('yt-legacy-state', 2),
                 ('yt-no-sample', 2), ('yt-unmeasured', 2), ('yt-failed', 1),
                 ('yt-unknown-and-failed', 1)]
        for fault, expected in cases:
            ru = verifier.run_verify(fault, youtube='zapret')
            for mode in ('full', 'single'):
                with self.subTest(fault=fault, mode=mode):
                    result = orchestrate(ru, foreign, mode)
                    if mode == 'full':
                        print(f'\n--- full deploy fixture: {fault or "healthy"} ---')
                        print(result.stdout, end='')
                        print(f'rc={result.returncode}')
                    self.assertEqual(result.returncode, expected, result.stdout)
                    if expected == 2:
                        self.assertIn('⚠', result.stdout)
                        reasons = [line for line in ru.stdout.splitlines() if '] ? ' in line]
                        self.assertTrue(reasons, ru.stdout)
                        for reason in reasons:
                            self.assertEqual(result.stdout.count(reason), 2 if mode == 'full' else 1)
                    if mode == 'full' and expected in (0, 2):
                        self.assertIn('деплой завершён', result.stdout)
                        payload = result.event.split("-d '", 1)[1].split("'", 1)[0]
                        self.assertEqual(json.loads(payload), {
                            'type': 'deploy_done', 'verify_unmeasured': expected == 2,
                        })
                    else:
                        self.assertEqual(result.event, '')
                    if expected == 1:
                        self.assertNotIn('деплой завершён', result.stdout)

    def test_failure_wins_in_both_host_orders_and_before_verify(self):
        unknown = subprocess.CompletedProcess([], 2, '[ru] ? yt — не проверено: HTTP 403\n')
        failed = subprocess.CompletedProcess([], 1, '[exit] ✗ sing-box active\n')
        healthy = subprocess.CompletedProcess([], 0, '[exit] ✓ sing-box active\n')
        for ru, foreign, failure in ((unknown, failed, ''), (failed, unknown, ''),
                                     (unknown, healthy, 'push'), (unknown, healthy, 'apt'),
                                     (unknown, healthy, 'other-stage')):
            with self.subTest(failure=failure, ru=ru.returncode):
                result = orchestrate(ru, foreign, failure=failure)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.event, '')
                self.assertNotIn('деплой завершён', result.stdout)

    def test_restore_distinguishes_unknown_from_failure(self):
        body = source('infra/lib/anysda-restore.sh')
        body = body[body.index('# ── Smoke-test'):body.index('# Данные восстановлены')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verify = root / 'verify'
            env = root / 'env'
            env.write_text('fixture')
            for code in (0, 1, 2, 255):
                verify.write_text(f'#!/bin/sh\nexit {code}\n')
                verify.chmod(0o755)
                script = f'STAGE_ENV={env}\nPRESNAP_FINAL=fixture\n' + body.replace(
                    '/opt/anysda-vpn2/infra/scripts/99-verify.sh', str(verify))
                result = subprocess.run(['bash', '-euc', script], text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0 if code in (0, 2) else 7, result.stderr)
                if code == 2:
                    self.assertIn('⚠', result.stderr)
                    self.assertNotIn('PASSED', result.stdout)


if __name__ == '__main__':
    unittest.main()
