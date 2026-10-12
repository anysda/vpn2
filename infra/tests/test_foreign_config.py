"""Execute the exit's real publication block with a simulated sing-box check."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))


class TestForeignConfigAudit(unittest.TestCase):
    def test_publish_only_after_validation(self):
        source = (ROOT / 'infra/scripts/10-foreign.sh').read_text()
        block = source[source.index('TPL=/tmp/anysda/'):source.index('cat > /etc/systemd/system/sing-box.service')]
        for valid in (False, True):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as tmp:
                # Use shell redirection to exercise the production block, including
                # its error handling, permissions and cleanup, without touching /etc.
                script = block.replace('/tmp/anysda', tmp).replace('/etc/sing-box', tmp)
                script = script.replace('/usr/local/bin/sing-box', 'check_config')
                fixture = '\n'.join([
                    'set -euo pipefail', 'HOST_TAG=test', 'umask 022',
                    f'cd {shlex.quote(tmp)}',
                    "printf '%s' old-config > config.json",
                    'chmod 600 config.json',
                    "printf '%s' new-config > sing-box-server.json.tpl",
                    'envsubst() { cat; }',
                    'python3() { :; }',
                    'check_config() {',
                    # Validation must observe the old published file until success.
                    '  test "$(cat config.json)" = old-config || return 71',
                    '  test "$(stat -c %a "$3")" = 600 || return 72',
                    '  test "$(cat "$3")" = new-config || return 73',
                    f'  return {0 if valid else 23}', '}', script,
                ])
                result = subprocess.run(['bash', '-c', fixture], text=True, capture_output=True)
                expected = 'new-config' if valid else 'old-config'
                self.assertEqual((Path(tmp) / 'config.json').read_text(), expected, result.stdout)
                self.assertEqual(result.returncode, 0 if valid else 1, result.stderr)
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                                 ['config.json', 'sing-box-server.json.tpl'])


if __name__ == '__main__':
    unittest.main()
