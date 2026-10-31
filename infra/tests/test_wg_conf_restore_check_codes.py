"""Restore names a mismatch only on code 3 of the check; any other failure is "not done", no reissue.

Review 15 of a8b6af8: anysda-restore.sh took every code of `--check-bundle` other than 0 and 5
for a mismatch and told to reissue devices. The check fails with 1, 2, 127 or a python
traceback without comparing anything. The check step of restore runs as it is, cut from the
script; the check it calls is a stub with the code, or the real one on a db.sqlite that is
not a database.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_wg_conf_allowedips_prefix import SERVER, cut, source
from test_wg_conf_module import LIB

NOT_DONE = 'anysda-restore: WARN: сверка БД с конфигами VPN не выполнена'
STUBS = {
    # name: (stub of anysda-backup.sh, code)
    'exit-1': ("echo 'boom' >&2\nexit 1\n", 1),
    'exit-2': ("echo 'Unknown option: -P' >&2\nexit 2\n", 2),
    'exit-127': ('python3-not-here -P - "$2"\n', 127),
}
REASON = 'нечем сверить - wg не ответил за 30 с'
PIECE = 9  # any piece of the key longer than 8 characters


def restore(check=None):
    """The check step of anysda-restore.sh with `check` as anysda-backup.sh (None - the real one).

    The bundle holds the server private key in wg0.conf and server.priv, db.sqlite is not a database.
    Returns (exit code, stderr, stdout).
    """
    block = cut(source('infra/lib/anysda-restore.sh'), 'CHECK=/usr/local/bin/anysda-backup.sh',
                '\n\n# Старые архивы')
    with tempfile.TemporaryDirectory() as directory:
        top = Path(directory)
        bundle = top / 'bundle'
        (bundle / 'etc-wireguard').mkdir(parents=True)
        (bundle / 'manifest.json').write_text('{"timestamp": "2026-10-30T120000Z"}')
        (bundle / 'db.sqlite').write_text('not a database\n')
        (bundle / 'etc-wireguard/wg0.conf').write_text(f'[Interface]\nPrivateKey = {SERVER}\nListenPort = 51820\n')
        (bundle / 'etc-wireguard/server.priv').write_text(SERVER + '\n')
        stub = top / 'anysda-backup.sh'
        stub.write_text(source('infra/lib/anysda-backup.sh') if check is None else '#!/bin/bash\n' + check)
        stub.chmod(0o755)
        assert block.count('/usr/local/bin/anysda-backup.sh') == 1, block
        script = top / 'anysda-restore.sh'
        script.write_text('#!/bin/bash\nset -euo pipefail\nBUNDLE_DIR=$1\n' +
                          block.replace('/usr/local/bin/anysda-backup.sh', str(stub)) + '\necho CHECKED\n')
        done = subprocess.run(['bash', str(script), str(bundle)], cwd=top,
                              env=os.environ | {'ANYSDA_LIB_DIR': str(LIB)}, capture_output=True, text=True,
                              timeout=60, check=False)
    assert 'CHECKED' in done.stdout, (done.stdout, done.stderr)
    return done.returncode, done.stderr, done.stdout


class RestoreCheckCodesTests(unittest.TestCase):
    def assert_not_done(self, err, code):
        self.assertIn(f'{NOT_DONE} (код {code}):', err)
        self.assertNotIn('перевыпусти', err)
        self.assertNotIn('расходятся', err)

    def test_1_failed_check_is_not_done_without_reissue(self):
        """1 exit 1, 2, 127: "не выполнена (код N)", no reissue, restore goes on with the exit of code 5."""
        five, *_ = restore(f"echo '{REASON}'\nexit 5\n")
        for name, (check, code) in STUBS.items():
            with self.subTest(name):
                rc, err, _ = restore(check)
                self.assert_not_done(err, code)
                self.assertEqual(rc, five, err)

    def test_1_traceback_is_not_done_without_reissue(self):
        """1 the real check on a db.sqlite that is not a database: traceback, "не выполнена (код 1)"."""
        five, *_ = restore(f"echo '{REASON}'\nexit 5\n")
        rc, err, _ = restore()
        self.assertIn('Traceback', err)
        self.assertIn('DatabaseError', err)
        self.assert_not_done(err, 1)
        self.assertEqual(rc, five, err)

    def test_2_mismatch_tells_to_reissue(self):
        """2 exit 3: "расходятся", reissue the devices named."""
        rc, err, _ = restore("echo 'устройство 7: нет пира WireGuard'\nexit 3\n")
        self.assertEqual(rc, 0, err)
        self.assertIn('anysda-restore: WARN: в архиве БД и пиры WireGuard расходятся — после восстановления '
                      'перевыпусти эти устройства в панели:\nустройство 7: нет пира WireGuard\n', err)
        self.assertNotIn('не выполнена', err)

    def test_2_nothing_to_check_with_names_the_reason(self):
        """2 exit 5: "не выполнена:" and the reason, no code, no reissue."""
        rc, err, _ = restore(f"echo '{REASON}'\nexit 5\n")
        self.assertEqual(rc, 0, err)
        self.assertIn(f'{NOT_DONE}:\n{REASON}\n', err)
        self.assertNotIn('(код', err)
        self.assertNotIn('перевыпусти', err)
        self.assertNotIn('расходятся', err)

    def test_2_check_passing_says_nothing(self):
        """2 exit 0: no warning."""
        rc, err, _ = restore("echo 'схема сверена'\nexit 0\n")
        self.assertEqual(rc, 0, err)
        self.assertNotIn('WARN', err)

    def test_3_check_output_carries_no_key(self):
        """3 codes 1, 2, 127, traceback, 3, 5: no piece of the key longer than 8 in restore's stdout or stderr."""
        checks = {name: check for name, (check, _) in STUBS.items()}
        checks |= {'traceback': None, 'exit-3': "echo 'устройство 7: нет пира WireGuard'\nexit 3\n",
                   'exit-5': f"echo '{REASON}'\nexit 5\n"}
        pieces = {SERVER[i:i + PIECE] for i in range(len(SERVER) - PIECE + 1)}
        for name, check in checks.items():
            with self.subTest(name):
                _, err, out = restore(check)
                if check is None:
                    self.assertIn('Traceback', err)
                for stream, text in (('stdout', out), ('stderr', err)):
                    leaked = sorted(piece for piece in pieces if piece in text)
                    self.assertEqual(leaked, [], f'{stream}: {text}')

if __name__ == '__main__':
    unittest.main()
