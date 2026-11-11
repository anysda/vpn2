from pathlib import Path
import tempfile
import unittest

from test_deploy import ROOT, bash


class Ikev2RotationTests(unittest.TestCase):
    def test_rotated_certificate_reloads_connection(self):
        helper = (ROOT / 'infra/ansible/roles/ikev2/files/anysda-ikev2-le-sync.sh').read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for child in ('x509', 'x509ca', 'private'):
                (root / child).mkdir()
            fixture = bash('openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 '
                           '-nodes -keyout key.pem -out leaf.pem -days 1 -subj /CN=vpn.example.test '
                           '-addext subjectAltName=DNS:vpn.example.test', directory)
            self.assertEqual(fixture.returncode, 0, fixture.stderr)
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
