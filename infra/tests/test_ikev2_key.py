from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from vpn_roles import package


class Ikev2KeyTests(unittest.TestCase):
    def test_failed_certificate_does_not_expose_ca_key(self):
        pki = package('pki')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modes = []

            def run(argv, **kwargs):
                if argv[:2] == ['pki', '--self']:
                    key = Path(argv[argv.index('--in') + 1])
                    modes.extend(oct(path.stat().st_mode & 0o777) for path in (key, key.parent))
                    return subprocess.CompletedProcess(argv, 23, '', 'boom')
                return pki.run(argv, **kwargs)
            umask = os.umask(0o022)
            try:
                with self.assertRaises(pki.PkiError) as raised, pki.transaction(root / 'pki') as work:
                    pki.build_ikev2(work, '203.0.113.1', '203.0.113.1', run=run)
            finally:
                os.umask(umask)
            self.assertEqual(raised.exception.code, 23)
            self.assertEqual(modes, ['0o600', '0o700'])
            self.assertFalse((root / 'pki').exists())
            self.assertEqual(list(root.glob('pki.tmp.*')), [])


if __name__ == '__main__':
    unittest.main()
