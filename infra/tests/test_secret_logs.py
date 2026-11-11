import tempfile
import unittest

from test_deploy import ROOT, bash


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


if __name__ == '__main__':
    unittest.main()
