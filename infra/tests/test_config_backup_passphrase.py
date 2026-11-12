"""Пустой backup.passphrase при включённом бэкапе отклоняется до записи env."""
from pathlib import Path
import tempfile
import unittest

from test_deploy import values, write_envs


CONFIG = """entry:
  host: 192.0.2.1
  password: 'entry secret'
exits:
  - tag: se
    host: 192.0.2.2
    password: 'exit secret'
"""


def backup(enabled, passphrase_line):
    lines = ['backup:', f'  enabled: {enabled}', '  backend: local']
    if passphrase_line is not None:
        lines.append(f'  {passphrase_line}')
    return CONFIG + '\n'.join(lines) + '\n'


class BackupPassphraseTests(unittest.TestCase):
    def convert(self, config, directory):
        (Path(directory) / 'config.yaml').write_text(config)
        return write_envs(directory, env={'HOME': directory})

    def env_text(self, directory):
        envs = Path(directory) / 'infra/envs'
        return ''.join(path.read_text() for path in sorted(envs.glob('*.env')))

    def assert_rejected(self, config):
        with tempfile.TemporaryDirectory() as directory:
            result = self.convert(config, directory)
            self.assertNotEqual(result.returncode, 0, 'пустой пароль бэкапа принят')
            self.assertIn('backup.passphrase', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertFalse((Path(directory) / 'infra/envs').exists(), 'env записаны')

    def test_empty_passphrase_rejected_when_enabled(self):
        for enabled in ('true', 'yes'):
            for line in ("passphrase: ''", 'passphrase: ""', 'passphrase:'):
                with self.subTest(enabled=enabled, line=line):
                    self.assert_rejected(backup(enabled, line))

    def test_blank_passphrase_accepted_when_enabled(self):
        for line, value in (("passphrase: '   '", '   '), ('passphrase: "\t "', '\t ')):
            with self.subTest(line=line), tempfile.TemporaryDirectory() as directory:
                result = self.convert(backup('true', line), directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                env = values(directory)
                self.assertEqual((env['BACKUP_ENABLED'], env['BACKUP_PASSPHRASE']), ('true', value))

    def test_missing_passphrase_rejected_when_enabled(self):
        self.assert_rejected(backup('true', None))

    def test_empty_passphrase_accepted_when_disabled(self):
        cases = [backup('false', "passphrase: ''"), backup('false', None), CONFIG]
        for config in cases:
            with self.subTest(config=config), tempfile.TemporaryDirectory() as directory:
                result = self.convert(config, directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('BACKUP_ENABLED', self.env_text(directory))

    def test_passphrase_accepted_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.convert(backup('true', "passphrase: 'long backup secret'"), directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = values(directory)
            self.assertEqual((env['BACKUP_ENABLED'], env['BACKUP_PASSPHRASE']), ('true', 'long backup secret'))


if __name__ == '__main__':
    unittest.main()
