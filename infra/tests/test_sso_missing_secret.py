from pathlib import Path
import shlex
import tempfile
import unittest
from unittest import mock

from containers_support import FACTS, compose, panel
from play_stage import real_site_tree
from test_deploy import bash, function, source


NOTICE = ('SSO не включён, потому что не задан секрет. Входите по паролю администратора. '
          'Чтобы включить SSO, заполните sso.client_secret в config.yaml '
          'и запустите ./deploy.sh 30-frontend ru.')
SECRET = 'test-only-sso-secret'


SEED = {'HOST_TAG': 'ru', 'ENTRY_HOST': '192.0.2.1', 'AGH_PORT': '3000', 'SSO_ENABLED': 'true',
        'SSO_PASSWORD_LOGIN': 'false', 'SSO_CLIENT_ID': 'vpn2', 'SSO_ALLOWED_SUBS': 'test-sub',
        'SSO_DISCOVERY_URL': 'https://auth.example.test/.well-known/openid-configuration'}


def panel_env(environ, runtime):
    """Решение стадии панели о SSO и окружение контейнера, которое из него выходит."""
    said = []
    with mock.patch.object(panel, 'say', said.append), \
            mock.patch.object(panel, 'SSO_RUNTIME', __file__ if runtime else '/nonexistent/sso-runtime.json'):
        facts = panel.sso(environ)
    return facts, compose.panel_environment(environ, {**FACTS, **facts}), '\n'.join(said)


class SsoMissingSecretTests(unittest.TestCase):
    def test_empty_secret_disables_sso_and_allows_password(self):
        for domain in ('vpn.example.test', ''):
            for secret in ('', '  ', None):
                with self.subTest(domain=domain, secret=secret):
                    environ = {**SEED, 'PANEL_DOMAIN': domain}
                    if secret is not None:
                        environ['SSO_CLIENT_SECRET'] = secret
                    facts, env, said = panel_env(environ, False)
                    self.assertEqual([facts['sso_enabled'], facts['sso_password_login'],
                                      facts['sso_redirect_url']], ['false', 'true', ''])
                    self.assertNotIn(NOTICE, said)
                    self.assertEqual(env['NUXT_PUBLIC_SSO_ENABLED'], 'false')
                    self.assertEqual(env['NUXT_PUBLIC_SSO_AUTO_REDIRECT'], 'false')
                    self.assertEqual(env['NUXT_SSO_PASSWORD_LOGIN'], 'true')
                    self.assertEqual(env['NUXT_OAUTH_OIDC_CLIENT_SECRET'], secret or '')

    def test_runtime_is_preserved_and_complete_seed_still_enables_sso(self):
        for runtime in (False, True):
            with self.subTest(runtime=runtime):
                environ = {**SEED, 'PANEL_DOMAIN': 'vpn.example.test',
                           'SSO_CLIENT_SECRET': '' if runtime else SECRET}
                facts, env, said = panel_env(environ, runtime)
                expected = ['false', 'true'] if runtime else ['true', 'false']
                self.assertEqual([facts['sso_enabled'], facts['sso_password_login']], expected)
                self.assertNotIn(SECRET, said)
                if runtime:
                    self.assertEqual(env['NUXT_PUBLIC_SSO_ENABLED'], 'false')
                    self.assertEqual(env['NUXT_PUBLIC_SSO_AUTO_REDIRECT'], 'false')
                    self.assertEqual(env['NUXT_SSO_PASSWORD_LOGIN'], 'true')

    def test_notice_is_once_at_end_of_pipeline_or_frontend_stage(self):
        deploy = source('infra/deploy.sh')
        functions = function('print_summary') + function('do_all') + function('stage_nodes')
        if 'print_sso_notice() {' in deploy:
            functions += function('print_sso_notice')
        dispatch = deploy.split('case "${1:-}" in', 1)[1]
        for mode in ('', '30-frontend ru', '23-emergency foreign', '99-verify all'):
            for runtime, enabled, secret in ((False, True, ''), (True, True, ''),
                                             (False, True, SECRET), (False, False, '')):
                with self.subTest(mode=mode, runtime=runtime, enabled=enabled, secret=secret):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        (root / 'envs').mkdir()
                        (root / 'lib').mkdir()
                        real_site_tree(root)
                        # Isolate notice placement; release acceptance is tested separately.
                        (root / 'lib/deploy-release.sh').write_text(
                            'release_start() { :; }\nrelease_complete() { :; }\n')
                        (root / 'envs/all.env').write_text('')
                        (root / 'envs/ru.env').write_text(
                            f'SSO_ENABLED={str(enabled).lower()}\nSSO_CLIENT_SECRET={shlex.quote(secret)}\n')
                        if runtime:
                            (root / 'sso-runtime.json').write_text('{"enabled":true}')
                        script = functions + '''
DEPLOY_ROOT=$PWD
C_G= C_B= C_END=
load_env() { source "$DEPLOY_ROOT/envs/ru.env"; }
die() { echo "die: $*" >&2; exit 1; }
ssh_exec() { bash -c "$1"; }
print_banner() { :; }
print_ikev2_notice() { :; }
pause_entry_auto_updates() { :; }
install_prereqs() { :; }
do_check() { :; }
preflight_ssh() { :; }
prep_orchestrator_key() { :; }
prep_entry_mgmt_key() { :; }
run_stage() { echo "STAGE $*"; }
run_tracks() { :; }
expand_hosts() { case "$1" in ru) echo ru ;; all) echo ru exit ;; *) echo exit ;; esac; }
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
