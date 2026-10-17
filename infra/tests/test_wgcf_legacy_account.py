"""Regression tests for wgcf accounts registered before key_type existed."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))


class WgcfLegacyAccountTests(unittest.TestCase):
    def run_stage(self, account, register_fails=False):
        source = (ROOT / 'infra/scripts/10-foreign.sh').read_text()
        start = source.index('cd /etc/anysda\n')
        end = source.index('\nif [[ "$WGCF_OK" == "1" ]] && [[ -f wgcf-profile.conf ]]; then', start)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'etc/anysda'
            state.mkdir(parents=True)
            (state / 'wgcf-account.toml').write_text(account)
            (state / 'wgcf-profile.conf').write_text('PrivateKey = previous\n')
            wgcf = root / 'wgcf'
            wgcf.write_text('''#!/bin/sh
case "$1" in
  generate)
    echo generate >> "$WGCF_CALLS"
    if ! grep -q key_type wgcf-account.toml; then
      echo 'no value given for required property key_type' >&2
      exit 1
    fi
    printf 'PrivateKey = fresh\\n' > wgcf-profile.conf
    ;;
  register)
    echo register >> "$WGCF_CALLS"
    echo 'Device Id: fixture-device-id'
    test "${WGCF_REGISTER_FAIL:-0}" = 0 || exit 1
    printf 'device_id = "new-device"\\nkey_type = "curve25519"\\n' > wgcf-account.toml
    ;;
esac
''')
            wgcf.chmod(0o755)
            calls = root / 'calls'
            block = source[start:end].replace('/etc/anysda', str(state)).replace(
                '/usr/local/bin/wgcf', str(wgcf))
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c',
                                     'HOST_TAG=test\n' + block], cwd=state, text=True,
                                    capture_output=True, env=dict(os.environ, WGCF_CALLS=str(calls),
                                                                  WGCF_REGISTER_FAIL=str(int(register_fails))))
            return result, calls.read_text().splitlines() if calls.exists() else [], {
                name: (state / name).read_text() for name in (
                    'wgcf-account.toml', 'wgcf-account.toml.old', 'wgcf-profile.conf')
                if (state / name).exists()
            }

    def test_legacy_account_is_reregistered_and_profile_replaced(self):
        result, calls, files = self.run_stage('device_id = "old-device"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ['generate', 'register', 'generate'])
        self.assertIn('key_type', files['wgcf-account.toml'])
        self.assertEqual(files['wgcf-account.toml.old'], 'device_id = "old-device"\n')
        self.assertEqual(files['wgcf-profile.conf'], 'PrivateKey = fresh\n')
        self.assertNotIn('оставляю профиль прошлого прогона', result.stdout)
        self.assertNotIn('fixture-device-id', result.stdout + result.stderr)

    def test_current_account_generates_without_reregistration_or_warning(self):
        result, calls, files = self.run_stage('device_id = "current-device"\nkey_type = "curve25519"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ['generate'])
        self.assertEqual(files['wgcf-profile.conf'], 'PrivateKey = fresh\n')
        self.assertNotIn('⚠', result.stdout)

    def test_register_failure_is_the_only_legacy_case_that_keeps_old_profile(self):
        result, calls, files = self.run_stage('device_id = "old-device"\n', register_fails=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ['generate', 'register'])
        self.assertEqual(files['wgcf-account.toml'], 'device_id = "old-device"\n')
        self.assertNotIn('wgcf-account.toml.old', files)
        self.assertEqual(files['wgcf-profile.conf'], 'PrivateKey = previous\n')
        self.assertIn('оставляю профиль прошлого прогона', result.stdout)


if __name__ == '__main__':
    unittest.main()
