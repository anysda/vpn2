"""Пароль AdGuard в роли adguard и в окружении панели: bcrypt и длинный UTF-8."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from adguard_support import stage


class AdguardPasswordTests(unittest.TestCase):
    def test_stage_and_panel_use_same_complete_password(self):
        for password in ('short-ASCII', 'a' * 72, 'a' * 73, 'a' * 256, 'a' * 300,
                         'я' * 36, 'я' * 37, '🔑' * 300, 'a' * 299 + 'b'):
            with self.subTest(length=len(password.encode())), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                (work / 'anysda').mkdir()
                user, backend, _ = stage.credentials({'ADMIN_PASSWORD': password}, work / 'anysda')
                digest = stage.new_hash(backend)
                panel = stage.agh_password(password)
                self.assertTrue(stage.hash_matches(user, panel, digest))
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
