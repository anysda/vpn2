import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT


class SsoRedeployTests(unittest.TestCase):
    def test_config_defers_seed_validation_to_entry(self):
        for seed in ('  base_url: https://auth.example.test\n  allowed_subs: test-sub\n', ''):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'config.yaml').write_text(
                    'entry:\n  host: 192.0.2.1\n  password: test-only\n'
                    'exits:\n  - tag: se\n    host: 192.0.2.2\n    password: test-only\n'
                    'sso:\n  enabled: true\n  client_secret: ""\n' + seed)
                result = subprocess.run(['python3', str(ROOT / 'infra/lib/config2env.py'), directory],
                                        text=True, capture_output=True, timeout=10,
                                        env={**os.environ, 'VPN2_STATE_DIR': f'{directory}/state'})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                env = (root / 'infra/envs/ru.env').read_text()
                self.assertIn("SSO_REQUESTED='true'", env)
                self.assertIn("SSO_ENABLED='false'", env)
                self.assertIn("SSO_AUTO_REDIRECT='false'", env)
                self.assertIn("SSO_PASSWORD_LOGIN='true'", env)
                self.assertIn("SSO_CLIENT_SECRET=''", env)
