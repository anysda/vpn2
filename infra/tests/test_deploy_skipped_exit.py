"""Недоступный экзит пропускается только на время полного деплоя."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT, function


def revision(path):
    return (ROOT / path).read_text()


def run(down_error='Connection refused', down_rc=1):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'envs').mkdir()
        (root / 'lib').mkdir()
        (root / 'emergency').mkdir()
        (root / 'emergency/provision.py').touch()
        (root / 'envs/all.env').write_text("EXIT_TAGS='ok down'\nENTRY_HOST=entry.example\n")
        (root / 'envs/exits.env').write_text("EXIT_TAGS='ok down'\n")
        for tag in ('ru', 'ok', 'down'):
            (root / f'envs/{tag}.env').write_text(f'HOST_TAG={tag}\n')
        (root / 'lib/ssh.sh').write_text(revision('infra/lib/ssh.sh'))
        (root / 'lib/anysda-svc.sh').write_text(revision('infra/lib/anysda-svc.sh'))
        deploy = revision('infra/deploy.sh')
        deploy = deploy[deploy.index('_STAGE_LOG='):deploy.index('# Без аргументов → полный pipeline.')]
        script = '''set -euo pipefail
DEPLOY_ROOT=$PWD
export DEPLOY_ROOT
source "$DEPLOY_ROOT/lib/ssh.sh"
''' + deploy + r'''
C_R= C_G= C_Y= C_B= C_END=
date() { echo 100; }
sleep() { :; }
load_env() { HOST_TAG=$1; SSH_USER=root; SSH_HOST=$1.example; SSH_PASS=fixture; }
ssh_probe() {
  if [[ $HOST_TAG == down ]]; then echo "$DOWN_ERROR"; return "$DOWN_RC"; fi
}
install_prereqs() { :; }
pause_entry_auto_updates() { :; }
prep_orchestrator_key() { :; }
prep_entry_mgmt_key() { :; }
prep_ru_router() { :; }
verify_and_rotate_ports() { :; }
prefetch_images() { true & _PREFETCH_PID=$!; }
print_ikev2_notice() { :; }
print_sso_notice() { :; }
announce_deploy_done() { :; }
run_stage_on_host() {
  if [[ $1 == 20-ru-router ]]; then
    source "$DEPLOY_ROOT/envs/all.env"
    printf '[20-ru-router] config exits: %s\n' "$EXIT_TAGS"
  else
    printf '[%s] deployed to %s\n' "$1" "$2"
  fi
}
do_all
'''
        result = subprocess.run(['bash', '-c', script], cwd=root, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env={**os.environ, 'PREFLIGHT_ATTEMPTS': '1',
                                     'PREFLIGHT_AUTH_PAUSE': '0', 'DOWN_ERROR': down_error,
                                     'DOWN_RC': str(down_rc)}, timeout=15)
        return result


class SkippedExitTests(unittest.TestCase):
    def test_exit_refusing_login_still_stops_deploy(self):
        result = run(down_error='root@down.example: Permission denied (publickey,password).',
                     down_rc=3)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('Экзит down пропущен', result.stdout)
        self.assertIn('Нода отказала во входе', result.stdout)
        self.assertIn('ключ anysda-orchestrator в /root/.ssh/authorized_keys', result.stdout)
        self.assertNotIn('[00-bootstrap] deployed', result.stdout)

    def test_unreachable_exit_does_not_stop_full_deploy(self):
        after = run()
        self.assertEqual(after.returncode, 0, after.stdout)
        self.assertIn('⚠ Экзит down пропущен: SSH недоступен в pre-flight', after.stdout)
        self.assertIn('⚠ Продолжаю без экзитов: down', after.stdout)
        self.assertIn('[00-bootstrap] deployed to ru', after.stdout)
        self.assertNotIn('deployed to down', after.stdout)
        self.assertIn('[20-ru-router] config exits: ok down', after.stdout)
        self.assertIn('[down] ⊘ пропущен: SSH недоступен в pre-flight', after.stdout)
        self.assertIn('[deploy] pre-flight: экзиты пропущены из-за недоступного SSH: down', after.stdout)

    def test_router_keeps_cached_secrets_for_skipped_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'secrets').mkdir()
            (root / 'secrets/foreign-secrets.env').write_text(
                ''.join(f'DOWN_{kind}=ABCDEFGHIJKLMNOP\n'
                        for kind in ('DIRECT', 'WARP', 'OBFS', 'MGMT', 'CLASH')))
            script = function('prep_ru_router') + r'''
DEPLOY_ROOT=$PWD
exit_tags() { echo 'ok down'; }
exit_is_skipped() { [[ $1 == down ]]; }
load_env() { HOST_TAG=$1; }
log() { :; }; ok() { :; }; warn() { :; }
die() { printf '%s\n' "$*" >&2; exit 1; }
ssh_exec() {
  case "$HOST_TAG:$*" in
    ok:*'echo "DIRECT='*)
      for key in DIRECT WARP OBFS MGMT CLASH; do
        printf '%s=QRSTUVWXYZABCDEF\n' "$key"
      done
      ;;
    *) return 1 ;;
  esac
}
prep_ru_router
'''
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=root,
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = (root / 'secrets/foreign-secrets.env').read_text()
            for tag, value in (('OK', 'QRSTUVWXYZABCDEF'), ('DOWN', 'ABCDEFGHIJKLMNOP')):
                for kind in ('DIRECT', 'WARP', 'OBFS', 'MGMT', 'CLASH'):
                    self.assertIn(f'{tag}_{kind}={value}\n', saved)


if __name__ == '__main__':
    unittest.main()
