"""Hysteria2 и служебный канал до экзита идут только с закреплённым сертификатом."""
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from router_support import ROOT, exit_cert, exit_secrets, generate, run_generator, stage
from test_deploy import bash, function, source


def hy2_tls(cfg):
    return {o['tag']: o['tls'] for o in cfg['outbounds'] if o['type'] == 'hysteria2'}


class ExitPinTests(unittest.TestCase):
    def test_exit_without_pinned_cert_is_refused(self):
        result = run_generator(AA_HY2_CERT_PATH='')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('экзит aa: нет закреплённого сертификата', result.stderr)
        self.assertIn('hy2_insecure', result.stderr)
        result = run_generator(BB_HY2_CERT_PATH='/nonexistent/bb.pem')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('экзит bb', result.stderr)

    def test_pinned_exits_have_no_insecure_anywhere(self):
        text = generate()
        self.assertNotIn('insecure', text)
        tls = hy2_tls(json.loads(text))
        self.assertTrue(any(tag.endswith('-mgmt') for tag in tls))
        for tag, settings in tls.items():
            self.assertEqual(settings.get('certificate_path'), exit_cert(), tag)

    def test_explicit_insecure_flag_is_the_only_compat_path(self):
        result = run_generator(AA_HY2_CERT_PATH='', AA_HY2_INSECURE='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('WARNING: экзит aa: exits[].hy2_insecure: true', result.stderr)
        for tag, settings in hy2_tls(json.loads(result.stdout)).items():
            self.assertEqual(settings.get('insecure', False), '-aa-' in tag, tag)

    def test_config_flag_reaches_env_and_deploy_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = (ROOT / 'config.example.yaml').read_text().replace(
                '  passphrase: ""', '  passphrase: test pass')
            (root / 'config.yaml').write_text(base.replace(
                '    host: 5.6.7.8\n', '    host: 5.6.7.8\n    hy2_insecure: true\n'))
            result = subprocess.run([sys.executable, str(ROOT / 'infra/lib/config2env.py'), tmp],
                                    capture_output=True, text=True,
                                    env={**os.environ, 'VPN2_STATE_DIR': f'{tmp}/state'})
            self.assertEqual(result.returncode, 0, result.stderr)
            env = (root / 'infra/envs/all.env').read_text()
            self.assertIn('US_HY2_INSECURE=1\n', env)
            self.assertNotIn('GB_HY2_INSECURE', env)
            script = function('warn_insecure_exits') + '''
DEPLOY_ROOT=$PWD/infra
exit_tags() { echo 'us gb'; }
warn() { printf '[%s] WARN %s\\n' "$HOST_TAG" "$*"; }
warn_insecure_exits
'''
            result = bash(script, tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('[deploy] WARN us: exits[].hy2_insecure: true', result.stdout)
            self.assertNotIn('gb:', result.stdout)
        stage = source('infra/deploy.sh')
        self.assertIn('[[ "$stage" != 99-verify ]] || warn_insecure_exits', stage)

    def test_deploy_pins_cert_that_exit_serves(self):
        # На установках без копии /etc/anysda/hy2-tls.crt сертификат берётся
        # тот, что отдаёт sing-box экзита; без него pin раньше молча не ставился.
        pem = Path(exit_cert()).read_text()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'served.pem').write_text(pem)
            script = function('prep_ru_router') + r'''
DEPLOY_ROOT=$PWD
exit_tags() { echo aa; }
exit_is_skipped() { return 1; }
load_env() { HOST_TAG=$1; }
log() { :; }; ok() { :; }; warn() { :; }
die() { printf '%s\n' "$*" >&2; exit 1; }
ssh_exec() {
  case "$*" in
    *'echo "DIRECT='*)
      for key in DIRECT WARP OBFS MGMT CLASH; do printf '%s=QRSTUVWXYZABCDEF\n' "$key"; done ;;
    *'test -f /etc/anysda/hy2-tls.crt'*) return 1 ;;
    *'cat /etc/sing-box/tls.crt'*) cat "$PWD/served.pem" ;;
    *) return 1 ;;
  esac
}
prep_ru_router
'''
            result = bash(script, tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            pinned = Path(tmp, 'secrets/exit-certs/aa.pem')
            self.assertEqual(pinned.read_text(), pem)
            self.assertEqual(pinned.stat().st_mode & 0o777, 0o600)

    def test_stage_keeps_pin_already_on_entry(self):
        # Повторный deploy без свежего сертификата (экзит пропущен, deploy с
        # другой машины) закрепляет уже лежащий на entry, а не падает.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pinned').mkdir()
            (root / 'pinned/aa.pem').write_text('old-pin')
            environ = {'EXIT_TAGS': 'aa bb', 'HY2_DIRECT_PORT': '443', 'HY2_MGMT_PORT': '3443'}
            env, _ = stage.exit_environment(environ, exit_secrets(['aa', 'bb']), root / 'incoming',
                                            root / 'pinned', 'lo')
            self.assertEqual(env['AA_HY2_CERT_PATH'], f'{root}/pinned/aa.pem')
            self.assertNotIn('BB_HY2_CERT_PATH', env)
            self.assertEqual((root / 'pinned/aa.pem').read_text(), 'old-pin')


if __name__ == '__main__':
    unittest.main()
