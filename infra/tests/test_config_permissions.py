import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_deploy import ROOT, bash, source


class ConfigPermissionTests(unittest.TestCase):
    def test_env_secrets_are_private_before_chmod(self):
        spec = importlib.util.spec_from_file_location('config2env', ROOT / 'infra/lib/config2env.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.yaml').write_text(
                'entry:\n  host: 192.0.2.1\n  password: CANARY_SECRET\n'
                'exits:\n  - tag: se\n    host: 192.0.2.2\n    password: CANARY_SECRET\n')
            original = Path.chmod

            def check_private(path, mode, **kwargs):
                if path.name in ('ru.env', 'se.env') and 'CANARY_SECRET' in path.read_text():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                return original(path, mode, **kwargs)

            mask = os.umask(0o022)
            try:
                with patch('sys.argv', ['config2env.py', directory]), patch.object(Path, 'chmod', check_private):
                    module.main()
                self.assertEqual((root / 'infra/envs/ru.env').stat().st_mode & 0o777, 0o600)
            finally:
                os.umask(mask)

    def test_setup_config_is_private_before_final_chmod(self):
        setup = source('setup.sh')
        code = setup.split("python3 <<'PYEOF'\n", 1)[1].split('\nPYEOF', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            result = bash('''
umask 022
export CONFIG_FILE="$PWD/config.yaml" ENTRY_IP=192.0.2.1 ENTRY_PASS=CANARY_SECRET
export EXITS_YAML='' ADMIN_USER=admin ADMIN_PASS=CANARY_SECRET
python3 - <<'PY'
''' + code + '\nPY\n', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(directory) / 'config.yaml').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
