"""Пароль AdGuard в роли adguard и в окружении панели: bcrypt и длинный UTF-8."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from adguard_support import stage


class AdguardPasswordTests(unittest.TestCase):
    def test_stage_and_panel_use_same_complete_password(self):
        for password in ('short-ASCII', 'a' * 72, 'a' * 73, 'a' * 256, 'a' * 300,
                         'я' * 36, 'я' * 37, '🔑' * 300, 'a' * 299 + 'b'):
            with self.subTest(length=len(password.encode())), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                (work / 'anysda').mkdir()
                with mock.patch.object(stage, 'ETC', str(work / 'anysda')), mock.patch.object(stage, 'UNITS', {}), \
                        mock.patch.object(stage, 'build_configs', lambda *_a, **_k: ()):
                    stage.configure({'ADMIN_PASSWORD': password, 'AGH_PORT': '3000'})
                backend = (work / 'anysda/adguard-password.txt').read_text()
                digest = stage.password_hash(None, 'anysda', backend)
                panel = stage.agh_password(password)
                self.assertEqual(stage.password_hash({'users': [{'name': 'anysda', 'password': digest}]}, 'anysda', panel), digest)
                expected = password.encode()
                if len(expected) > 72:
                    expected = hashlib.sha256(b'vpn2-adguard-v1\0' + expected).hexdigest().encode()
                self.assertEqual(backend.encode(), expected)
                self.assertEqual(panel.encode(), expected)
                self.assertEqual((work / 'anysda/admin-password.txt').read_text(), password)
                saved = work / 'anysda/adguard-password.txt'
                self.assertEqual(saved.read_bytes(), expected)
                self.assertEqual(saved.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
