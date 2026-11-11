import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_deploy import ROOT


class ConfigPermissionTests(unittest.TestCase):
    def test_env_secrets_are_private_before_write(self):
        spec = importlib.util.spec_from_file_location('config2env', ROOT / 'infra/lib/config2env.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                self.check_env_write(module, directory, existing)

    def check_env_write(self, module, directory, existing):
        root = Path(directory)
        (root / 'config.yaml').write_text(
            'entry:\n  host: 192.0.2.1\n  password: CANARY_SECRET\n'
            'exits:\n  - tag: se\n    host: 192.0.2.2\n    password: CANARY_SECRET\n')
        if existing:
            envs = root / 'infra/envs'
            envs.mkdir(parents=True)
            for name in ('ru.env', 'se.env'):
                path = envs / name
                path.write_text('OLD_CONFIG=1\n')
                path.chmod(0o644)
        original = Path.write_text
        written = []

        def check_private(path, data, **kwargs):
            if path.name in ('ru.env', 'se.env') and 'CANARY_SECRET' in data:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                written.append(path.name)
            return original(path, data, **kwargs)

        mask = os.umask(0o022)
        try:
            with patch('sys.argv', ['config2env.py', directory]), patch.object(Path, 'write_text', check_private), \
                    patch.dict(os.environ, VPN2_STATE_DIR=f'{directory}/state'):
                module.main()
            self.assertCountEqual(written, ['ru.env', 'se.env'])
            for name in written:
                self.assertIn('CANARY_SECRET', (root / 'infra/envs' / name).read_text())
        finally:
            os.umask(mask)


if __name__ == '__main__':
    unittest.main()
