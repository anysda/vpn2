import hashlib
import json
from pathlib import Path
import shlex
import tempfile
import unittest

from test_deploy import bash, function, source


NOTICE = ('SSO не включён, потому что не задан секрет. Входите по паролю администратора. '
          'Чтобы включить SSO, заполните sso.client_secret в config.yaml '
          'и запустите ./deploy.sh 30-frontend ru.')
SECRET = 'test-only-sso-secret'


def seed_block():
    return source('infra/scripts/30-frontend.sh').split(
        '# --- SSO (Authentik / OIDC)', 1)[1].split('\nTPL=', 1)[0].split('\n', 1)[1]


def container_block():
    stage = source('infra/scripts/30-frontend.sh')
    return '''
ENTRY_HOST=192.0.2.1 EXIT_TAGS='' MGMT_IP=192.0.2.1 AGH_PORT=3000
ADMIN_USER=admin ADMIN_PASS=test-only-password SESSION_SECRET=test-only-session
NODE_HOSTS=ru=192.0.2.1 PANEL_IMAGE=test-only-image
docker() { printf '%s\\n' "$@" > docker.args; }
docker run -d \\
''' + stage.split('docker run -d \\\n', 1)[1].split('\necho ', 1)[0]


class SsoMissingSecretTests(unittest.TestCase):
    def test_empty_secret_disables_sso_and_allows_password(self):
        for domain in ('vpn.example.test', ''):
            for secret in ("SSO_CLIENT_SECRET=''", 'unset SSO_CLIENT_SECRET'):
                with self.subTest(domain=domain, secret=secret), tempfile.TemporaryDirectory() as directory:
                    result = bash(f'''
HOST_TAG=ru
SSO_ENABLED=true
SSO_PASSWORD_LOGIN=false
SSO_CLIENT_ID=vpn2
SSO_DISCOVERY_URL=https://auth.example.test/.well-known/openid-configuration
SSO_ALLOWED_SUBS=test-sub
PANEL_DOMAIN={shlex.quote(domain)}
{secret}
''' + (seed_block() + container_block()).replace('/etc/anysda', directory) + '''
printf 'effective=%s password=%s redirect=%s\n' "$SSO_ENABLED" "$SSO_PASSWORD_LOGIN" "$SSO_REDIRECT_URL"
''', directory)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('effective=false password=true redirect=', result.stdout)
                    self.assertNotIn(NOTICE, result.stdout + result.stderr)
                    self.assertFalse((Path(directory) / 'sso-runtime.json').exists())
                    args = (Path(directory) / 'docker.args').read_text().splitlines()
                    self.assertIn('NUXT_PUBLIC_SSO_ENABLED=false', args)
                    self.assertIn('NUXT_SSO_PASSWORD_LOGIN=true', args)
                    self.assertIn('NUXT_OAUTH_OIDC_CLIENT_SECRET=', args)

    def test_runtime_is_preserved_and_complete_seed_still_enables_sso(self):
        for runtime in (None, {'enabled': True, 'passwordLogin': False, 'clientSecret': SECRET}):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                stored = Path(directory) / 'sso-runtime.json'
                if runtime:
                    stored.write_text(json.dumps(runtime))
                    before = hashlib.sha256(stored.read_bytes()).hexdigest()
                result = bash(f'''
HOST_TAG=ru
SSO_ENABLED=true
SSO_PASSWORD_LOGIN=false
PANEL_DOMAIN=vpn.example.test
SSO_CLIENT_ID=vpn2
SSO_CLIENT_SECRET={shlex.quote('' if runtime else SECRET)}
SSO_DISCOVERY_URL=https://auth.example.test/.well-known/openid-configuration
SSO_ALLOWED_SUBS=test-sub
''' + seed_block().replace('/etc/anysda', directory) + '''
printf 'effective=%s password=%s\n' "$SSO_ENABLED" "$SSO_PASSWORD_LOGIN"
''', directory)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('effective=true password=false', result.stdout)
                self.assertNotIn(SECRET, result.stdout + result.stderr)
                if runtime:
                    self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), before)

    def test_notice_is_once_at_end_of_pipeline_or_frontend_stage(self):
        deploy = source('infra/deploy.sh')
        functions = function('print_summary') + function('do_all')
        if 'print_sso_notice() {' in deploy:
            functions += function('print_sso_notice')
        dispatch = deploy.split('case "${1:-}" in', 1)[1]
        for mode in ('', '30-frontend ru', '30-frontend foreign', '99-verify all'):
            for runtime, enabled, secret in ((False, True, ''), (True, True, ''),
                                             (False, True, SECRET), (False, False, '')):
                with self.subTest(mode=mode, runtime=runtime, enabled=enabled, secret=secret):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        (root / 'envs').mkdir()
                        (root / 'envs/all.env').write_text('')
                        (root / 'envs/ru.env').write_text(
                            f'SSO_ENABLED={str(enabled).lower()}\nSSO_CLIENT_SECRET={shlex.quote(secret)}\n')
                        if runtime:
                            (root / 'sso-runtime.json').write_text('{"enabled":true}')
                        script = functions + '''
DEPLOY_ROOT=$PWD
C_G= C_B= C_END=
load_env() { source "$DEPLOY_ROOT/envs/ru.env"; }
ssh_exec() { bash -c "$1"; }
print_banner() { :; }
print_ikev2_notice() { :; }
pause_entry_auto_updates() { :; }
install_prereqs() { :; }
do_check() { :; }
preflight_ssh() { :; }
run_stage() { echo "STAGE $*"; }
run_tracks() { :; }
verify_and_rotate_ports() { :; }
prefetch_images() { _PREFETCH_PID=0; }
wait() { :; }
announce_deploy_done() { :; }
''' + f'set -- {mode}\ncase "${{1:-}}" in' + dispatch
                        result = bash(script.replace('/etc/anysda', directory), directory)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        expected = enabled and not secret and not runtime and mode in ('', '30-frontend ru')
                        self.assertEqual(result.stdout.count(NOTICE), int(expected), result.stdout)
                        if expected:
                            self.assertTrue(result.stdout.rstrip().endswith(NOTICE), result.stdout)
                            self.assertIn('STAGE ' + (mode or '99-verify all'), result.stdout)
                        self.assertNotIn(SECRET, result.stdout + result.stderr)
