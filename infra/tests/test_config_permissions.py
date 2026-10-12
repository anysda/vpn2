import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_deploy import ROOT, source


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
            with patch('sys.argv', ['config2env.py', directory]), patch.object(Path, 'write_text', check_private):
                module.main()
            self.assertCountEqual(written, ['ru.env', 'se.env'])
            for name in written:
                self.assertIn('CANARY_SECRET', (root / 'infra/envs' / name).read_text())
        finally:
            os.umask(mask)

    def test_setup_config_is_private_before_write(self):
        setup = source('setup.sh')
        code = setup.split("python3 <<'PYEOF'\n", 1)[1].split('\nPYEOF', 1)[0]
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'config.yaml'
                if existing:
                    target.write_text('old config\n')
                    target.chmod(0o644)
                original = os.fdopen
                written = []

                def private_fdopen(*args, **kwargs):
                    stream = original(*args, **kwargs)
                    write = stream.write

                    def private_write(data):
                        self.assertEqual(os.fstat(stream.fileno()).st_mode & 0o777, 0o600)
                        written.append(data)
                        return write(data)

                    stream.write = private_write
                    return stream

                env = dict(CONFIG_FILE=str(target), ENTRY_IP='192.0.2.1', ENTRY_PASS='CANARY_SECRET',
                           EXITS_YAML='', ADMIN_USER='admin', ADMIN_PASS='CANARY_SECRET')
                mask = os.umask(0o022)
                try:
                    with patch.dict(os.environ, env, clear=True), patch('os.fdopen', private_fdopen):
                        exec(compile(code, 'setup.sh', 'exec'), {})
                    self.assertEqual(len(written), 1)
                    self.assertIn('CANARY_SECRET', target.read_text())
                    self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                finally:
                    os.umask(mask)


if __name__ == '__main__':
    unittest.main()
