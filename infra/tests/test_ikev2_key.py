from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, source


class Ikev2KeyTests(unittest.TestCase):
    def test_failed_certificate_does_not_expose_ca_key(self):
        stage = source('infra/scripts/27-ikev2.sh')
        block = stage.split('  if [[ ! -f "$PKI/ca.key" ]]; then', 1)[1].split('\n  fi', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            result = bash('''
umask 022
HOST_TAG=ru
PKI=$PWD
pki() { if [[ $1 == --gen ]]; then echo CANARY_PRIVATE_KEY; else return 23; fi; }
''' + block, directory)
            self.assertEqual(result.returncode, 23)
            self.assertEqual((Path(directory) / 'ca.key').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
