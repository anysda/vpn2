"""Стадия панели (vpn2.containers.panel): решение о SSO и окружение контейнера, что из него выходит."""
import unittest
from unittest import mock

from vpn2.containers import compose, panel


NOTICE = ('SSO не включён, потому что не задан секрет. Входите по паролю администратора. '
          'Чтобы включить SSO, заполните sso.client_secret в config.yaml '
          'и запустите ./deploy.sh 30-frontend ru.')
SECRET = 'test-only-sso-secret'
# Секреты и решения стадии, которых нет в значениях узла.
FACTS = {'session': 's', 'clash': 'c', 'tgbot': 't', 'admin_user': 'admin', 'agh_password': 'p',
         'sso_enabled': 'false', 'sso_auto_redirect': 'false', 'sso_password_login': 'true', 'sso_redirect_url': ''}


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
