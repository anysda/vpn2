from pathlib import Path
import tempfile
import unittest

from test_deploy import bash, source


class Ikev2KeyTests(unittest.TestCase):
    def test_failed_certificate_does_not_expose_ca_key(self):
        stage = source('infra/scripts/27-ikev2.sh')
        builder = stage.split('  ikev2_pki_build() {', 1)[1].split('\n  }\n', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            result = bash(source('infra/lib/anysda-pki.sh') + '''
umask 022
HOST_TAG=ru
ENTRY_HOST=203.0.113.1
pki() {
  if [[ $1 == --self ]]; then
    stat -c '%a' "$3" "$(dirname "$3")" > "$PWD/modes"
    return 23
  fi
}
ikev2_pki_build() {''' + builder + '''
}
pki_transaction "$PWD/pki" ikev2_pki_build
''', directory)
            root = Path(directory)
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual((root / 'modes').read_text().split(), ['600', '700'])
            self.assertFalse((root / 'pki').exists())
            self.assertEqual(list(root.glob('pki.tmp.*')), [])


if __name__ == '__main__':
    unittest.main()
