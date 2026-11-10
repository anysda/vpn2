"""Bootstrap puts the vpn2 wheel into /opt/vpn2/venv on an exit host.

The step is the block between the "venv vpn2" marker comments in
00-bootstrap.sh. The test cuts it out, points the wheel, venv and pip
directories into a temporary tree and replaces python3 in PATH with a recorder.
"""
import hashlib
from pathlib import Path
import re
import tempfile
import unittest

from test_deploy import bash, source

STUB = r'''#!/usr/bin/env bash
echo "python3 $*" >> "$FIX/calls"
[[ -z "${FAIL_PIP:-}" || "$*" != *" install "* ]] || exit 1
if [[ "${1:-}" == -m && "${2:-}" == venv ]]; then
  mkdir -p "${@: -1}"
fi
'''


def venv_block():
    text = source('infra/scripts/00-bootstrap.sh')
    match = re.search(r'^# --- venv vpn2: начало\n.*?^# --- venv vpn2: конец\n',
                      text, re.M | re.S)
    assert match, 'no venv block in 00-bootstrap.sh'
    return match.group(0)


class BootstrapVenvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for sub in ('bin', 'anysda', 'opt/vpn2/venv', 'pywheels/pip-1.whl'):
            (self.root / sub).mkdir(parents=True)
        stub = self.root / 'bin/python3'
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.wheel = self.root / 'anysda/vpn2-0.1.0-py3-none-any.whl'
        self.sha = hashlib.sha256(b'wheel').hexdigest()
        self.marker = self.root / 'opt/vpn2/venv/.vpn2-wheel'

    def put_wheel(self):
        self.wheel.write_bytes(b'wheel')

    def run_block(self, fail_pip=False):
        script = (f'export FIX={self.root}\nexport PATH=$FIX/bin:$PATH\n'
                  f'export VPN2_ROOT={self.root}/opt/vpn2 VPN2_WHEEL_DIR={self.root}/anysda\n'
                  f'export VPN2_PIP_DIR={self.root}/pywheels\n'
                  + ('export FAIL_PIP=1\n' if fail_pip else '')
                  + 'HOST_TAG=nl\n' + venv_block())
        return bash(script, self.root)

    def calls(self):
        path = self.root / 'calls'
        return path.read_text().splitlines() if path.exists() else []

    def test_no_marker_installs_and_writes_marker(self):
        self.put_wheel()
        (self.root / 'opt/vpn2/venv').rmdir()
        result = self.run_block()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 2, calls)
        self.assertIn('-m venv --without-pip --system-site-packages --clear', calls[0])
        self.assertIn('--no-index --no-deps', calls[1])
        self.assertIn('vpn2-0.1.0-py3-none-any.whl', calls[1])
        self.assertEqual(self.marker.read_text().strip(), self.sha)

    def test_same_marker_does_nothing(self):
        self.put_wheel()
        self.marker.write_text(self.sha + '\n')
        result = self.run_block()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_other_marker_reinstalls(self):
        self.put_wheel()
        self.marker.write_text('0' * 64 + '\n')
        result = self.run_block()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.marker.read_text().strip(), self.sha)

    def test_no_wheel_skips_with_log_line(self):
        result = self.run_block()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertIn('колесо vpn2 не передано', result.stdout)

    def test_pip_failure_fails_without_marker(self):
        self.put_wheel()
        result = self.run_block(fail_pip=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())
        self.assertIn('не удалось поставить колесо', result.stderr)


if __name__ == '__main__':
    unittest.main()
