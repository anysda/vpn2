"""Реальные credentials стадии 22 и env Docker стадии 30: bcrypt и длинный UTF-8."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_deploy import ROOT, source


class AdguardPasswordTests(unittest.TestCase):
    def test_stage_and_panel_use_same_complete_password(self):
        script = source('infra/scripts/22-adguard.sh')
        credentials = script.split('AGH_USER=', 1)[1].split('# ----------------------------------------------------------------------------', 1)[0]
        frontend = source('infra/scripts/30-frontend.sh')
        argument = next(line.strip().removesuffix(' \\') for line in frontend.splitlines()
                        if '-e NUXT_AGH_PASSWORD=' in line)
        for password in ('short-ASCII', 'a' * 72, 'a' * 73, 'a' * 256, 'a' * 300,
                         'я' * 36, 'я' * 37, '🔑' * 300, 'a' * 299 + 'b'):
            with self.subTest(length=len(password.encode())), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                (work / 'anysda').mkdir()
                block = ('AGH_USER=' + credentials).replace('/etc/anysda', str(work / 'anysda'))
                env = dict(os.environ, ADMIN_PASSWORD=password, HOST_TAG='ru', AGH_DIR=directory,
                           ADMIN_PASS=password, TMPDIR=directory)
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c',
                    f'source "{ROOT}/infra/lib/anysda-svc.sh"\n' + block + '\n'
                    'printf "%s" "$AGH_PASS" > "$AGH_DIR/backend-password"\n'
                    'capture() { printf "%s" "${2#NUXT_AGH_PASSWORD=}" > "$AGH_DIR/panel-password"; }\n'
                    'capture ' + argument + '\n'
                    'printf "%s:%s\\n" "$AGH_USER" "$AGH_PASS_HASH" > "$AGH_DIR/check.htpasswd"\n'
                    'htpasswd -vb "$AGH_DIR/check.htpasswd" "$AGH_USER" "$(cat "$AGH_DIR/panel-password")"\n'],
                    env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = (work / 'backend-password').read_bytes()
                expected = password.encode()
                if len(expected) > 72:
                    expected = hashlib.sha256(b'vpn2-adguard-v1\0' + expected).hexdigest().encode()
                self.assertEqual(actual, expected)
                self.assertEqual((work / 'panel-password').read_bytes(), expected)
                self.assertEqual((work / 'anysda/admin-password.txt').read_text(), password)
                saved = work / 'anysda/adguard-password.txt'
                if saved.exists():
                    self.assertEqual(saved.read_bytes(), expected)
                    self.assertEqual(saved.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
