from pathlib import Path
import shlex
import tempfile
import unittest

from test_deploy import ROOT, bash, source

LIB = shlex.quote(str(ROOT / 'infra/lib/anysda-pki.sh'))


class Ikev2ExpiryTests(unittest.TestCase):
    def test_expired_certificate_is_reissued_and_valid_certificate_is_kept(self):
        stage = source('infra/scripts/27-ikev2.sh')
        block = '  ikev2_pki_build() {' + stage.split('  ikev2_pki_build() {', 1)[1].split(
            '  # Снять watcher', 1)[0]
        for days, host, renew in ((-1, '192.0.2.1', True), (30, '192.0.2.1', False),
                                  (30, '192.0.2.2', True)):
            with self.subTest(days=days, host=host), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for child in ('x509', 'x509ca', 'private'):
                    (root / child).mkdir()
                (root / 'extensions').write_text(f'subjectAltName=IP:{host}\n')
                (root / 'index.txt').touch()
                (root / 'serial').write_text('01\n')
                (root / 'ca.cnf').write_text('''[ca]
default_ca=test
[test]
database=index.txt
serial=serial
new_certs_dir=.
certificate=ca.crt
private_key=ca.key
default_md=sha256
policy=policy
[policy]
commonName=supplied
''')
                issue = ('openssl ca -batch -notext -config ca.cnf -startdate 20000101000000Z '
                         '-enddate 20010101000000Z -in server.csr -extfile extensions -out server.crt'
                         if days < 0 else
                         'openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -set_serial 1 '
                         '-days 30 -extfile extensions -out server.crt')
                fixture = bash(f'''
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout ca.key -out ca.crt -days 365 -subj /CN=test-ca
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout server.key -out server.csr -subj /CN=test-server
{issue}
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -set_serial 2 \
  -days 30 -extfile extensions -out renewed.crt
''', directory)
                self.assertEqual(fixture.returncode, 0, fixture.stderr)
                original = (root / 'server.crt').read_bytes()
                ca = (root / 'ca.crt').read_bytes()
                before = bash('openssl x509 -in server.crt -noout -checkend 0', directory)
                self.assertEqual(before.returncode == 0, days > 0)
                pki = root / 'pki'
                pki.mkdir()
                for name in ('ca.key', 'ca.crt', 'server.key', 'server.crt', 'renewed.crt'):
                    (pki / name).write_bytes((root / name).read_bytes())
                script = f'source {LIB}\n' + '''
PKI=$PWD/pki
HOST_TAG=ru
ENTRY_HOST=192.0.2.1
pki() {
  echo "$1" >> issued
  case "$1" in
    --gen) cat "$PKI/server.key" ;;
    --pub) openssl pkey -in "$PKI/server.key" -pubout ;;
    --issue) cat "$PKI/renewed.crt" ;;
    *) return 1 ;;
  esac
}
''' + block.replace('/etc/swanctl', shlex.quote(directory))
                result = bash(script, directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                installed = (root / 'x509/anysda-server.crt').read_bytes()
                expected = (root / 'renewed.crt').read_bytes() if renew else original
                self.assertEqual(installed, expected, 'expired or wrong-SAN certificate was kept')
                self.assertEqual((root / 'issued').exists(), renew)
                self.assertEqual((root / 'ca.crt').read_bytes(), ca)
                self.assertEqual((root / 'private/anysda-server.key').stat().st_mode & 0o777, 0o600)
                after = bash('openssl x509 -in x509/anysda-server.crt -noout -checkend 0', directory)
                self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
