import tempfile
import unittest

from test_deploy import ROOT, bash, source


class SecretLogTests(unittest.TestCase):
    def test_yaml_error_hides_value(self):
        with tempfile.TemporaryDirectory() as directory:
            result = bash('''
python3 - "$1" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location('config2env', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.parse_yaml("entry:\\n  password: 'CANARY_UNCLOSED_SECRET\\n")
PY
'''.replace('"$1"', f'"{ROOT}/infra/lib/config2env.py"'), directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('незакрытая кавычка', result.stderr)
            self.assertNotIn('CANARY_UNCLOSED_SECRET', result.stdout + result.stderr)

    def test_generated_backup_passphrase_not_logged(self):
        setup = source('setup.sh')
        start = setup.index('  if [[ -z "$backup_passphrase" ]]; then')
        block = setup[start:setup.index('\n  fi', start) + len('\n  fi')]
        with tempfile.TemporaryDirectory() as directory:
            result = bash('''
backup_passphrase=''
python3() { echo CANARY_GENERATED_SECRET; }
warn() { echo "$*" >&2; }
''' + block, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('CANARY_GENERATED_SECRET', result.stdout + result.stderr)
            self.assertIn('config.yaml', result.stderr)


if __name__ == '__main__':
    unittest.main()
