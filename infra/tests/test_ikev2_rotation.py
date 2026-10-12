from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, source


class Ikev2RotationTests(unittest.TestCase):
    def test_rotated_certificate_reloads_connection(self):
        helper = source('infra/scripts/27-ikev2.sh').split("<<'LESYNC'\n", 1)[1].split('\nLESYNC', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for child in ('x509', 'x509ca', 'private'):
                (root / child).mkdir()
            (root / 'leaf.pem').write_text('-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n')
            (root / 'key.pem').write_text('test-only-key\n')
            helper = helper.replace('/etc/swanctl', directory)
            result = bash('''
set -- leaf.pem key.pem
swanctl() {
  echo "$*" >> calls
  if [[ $1 == --load-conns ]]; then echo new > connection-cert; fi
}
echo old > connection-cert
''' + helper, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'connection-cert').read_text().strip(), 'new')
            self.assertEqual((root / 'calls').read_text().splitlines(), ['--load-creds', '--load-conns'])


if __name__ == '__main__':
    unittest.main()
