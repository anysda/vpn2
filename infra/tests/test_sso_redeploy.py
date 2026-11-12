from pathlib import Path
import tempfile
import unittest

from test_deploy import values, write_envs


class SsoRedeployTests(unittest.TestCase):
    def test_config_defers_seed_validation_to_entry(self):
        for seed in ('  base_url: https://auth.example.test\n  allowed_subs: test-sub\n', ''):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'config.yaml').write_text(
                    'entry:\n  host: 192.0.2.1\n  password: test-only\n'
                    'exits:\n  - tag: se\n    host: 192.0.2.2\n    password: test-only\n'
                    'sso:\n  enabled: true\n  client_secret: ""\n' + seed)
                result = write_envs(directory)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                env = values(directory)
                self.assertEqual({name: env.get(name) for name in ('SSO_REQUESTED', 'SSO_ENABLED', 'SSO_AUTO_REDIRECT',
                                                                   'SSO_PASSWORD_LOGIN', 'SSO_CLIENT_SECRET')},
                                 {'SSO_REQUESTED': 'true', 'SSO_ENABLED': 'false', 'SSO_AUTO_REDIRECT': 'false',
                                  'SSO_PASSWORD_LOGIN': 'true', 'SSO_CLIENT_SECRET': ''})
