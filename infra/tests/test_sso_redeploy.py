import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT, bash, source


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
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                env = (root / 'infra/envs/ru.env').read_text()
                self.assertIn("SSO_ENABLED='true'", env)
                self.assertIn("SSO_CLIENT_SECRET=''", env)

    def test_runtime_takes_priority_and_missing_runtime_still_validates_seed(self):
        stage = source('infra/scripts/30-frontend.sh')
        block = stage.split('# --- SSO (Authentik / OIDC)', 1)[1].split('\nTPL=', 1)[0]
        block = block.split('\n', 1)[1]
        good = {'SSO_ENABLED': 'true', 'PANEL_DOMAIN': 'vpn.example.test',
                'SSO_DISCOVERY_URL': 'https://auth.example.test/.well-known/openid-configuration',
                'SSO_CLIENT_ID': 'vpn2', 'SSO_CLIENT_SECRET': 'test-only-secret',
                'SSO_ALLOWED_SUBS': 'test-sub'}
        for runtime in (None, {'enabled': True, 'passwordLogin': False}, {'enabled': False}):
            for empty in (None, 'SSO_CLIENT_SECRET', 'SSO_DISCOVERY_URL', 'SSO_ALLOWED_SUBS', 'PANEL_DOMAIN'):
                with self.subTest(runtime=runtime, empty=empty), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    stored = root / 'sso-runtime.json'
                    if runtime is not None:
                        stored.write_text(json.dumps(runtime))
                    env = good | ({empty: ''} if empty else {})
                    script = 'HOST_TAG=ru\n' + '\n'.join(f"{key}='{value}'" for key, value in env.items())
                    result = bash(script + '\n' + block.replace('/etc/anysda', directory), directory)
                    if runtime is not None or empty in (None, 'SSO_CLIENT_SECRET'):
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0, 'first-install validation was skipped')
                    if runtime is not None:
                        self.assertEqual(stored.read_text(), json.dumps(runtime))
                    self.assertNotIn('test-only-secret', result.stdout + result.stderr)
