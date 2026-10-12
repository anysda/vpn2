"""Запуск всего мастера со stdin и PTY; сетевые ответы фиксированы."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest

from test_deploy import ROOT


COMMAND = '''
ip() { printf 'default via 192.0.2.254 dev eth0\\n    inet 192.0.2.1/24 scope global eth0\\n'; }
curl() { printf '{}'; }
dig() { printf '192.0.2.1\\n'; }
export -f ip curl dig
exec bash setup.sh
'''


def prepare(directory):
    root = Path(directory)
    shutil.copy(ROOT / 'setup.sh', root)
    (root / 'infra/lib').mkdir(parents=True)
    shutil.copy(ROOT / 'infra/lib/config2env.py', root / 'infra/lib')
    return root


def run_setup(directory, answers):
    with subprocess.Popen(['bash', '-c', COMMAND], cwd=directory,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, start_new_session=True, env={**os.environ, 'HOME': directory}) as process:
        try:
            output, _ = process.communicate(answers, timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise AssertionError('мастер не завершился после EOF') from None
        return process.returncode, output


class SetupInputTests(unittest.TestCase):
    def test_password_whitespace_survives_stdin_and_env(self):
        secret = '  CANARY \'" $HOME `true` \\ tail\t '
        answers = ['1', secret, '192.0.2.2', secret, 'se', '', secret, secret,
                   '', 'n', 'n', 'y', 'n', 'local', secret, 'n', 'y']
        with tempfile.TemporaryDirectory() as directory:
            root = prepare(directory)
            code, output = run_setup(directory, '\n'.join(answers) + '\n')
            self.assertEqual(code, 0, output)
            result = subprocess.run(['bash', '-euc',
                                     'source infra/envs/ru.env; '
                                     'printf "%s\\0" "$SSH_PASS" "$ADMIN_PASSWORD" "$BACKUP_PASSPHRASE"; '
                                     'source infra/envs/se.env; printf "%s\\0" "$SSH_PASS"'],
                                    cwd=root, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.split('\0')[:-1], [secret] * 4)
            self.assertNotIn('CANARY', output)
            for path in ('config.yaml', 'infra/envs/ru.env', 'infra/envs/se.env'):
                self.assertEqual((root / path).stat().st_mode & 0o777, 0o600)

    def test_eof_aborts_without_writing_config(self):
        for answers in ('', '1\n', '1\nroot secret\n'):
            with self.subTest(answers=answers), tempfile.TemporaryDirectory() as directory:
                root = prepare(directory)
                code, output = run_setup(directory, answers)
                self.assertNotEqual(code, 0, output)
                self.assertIn('EOF', output)
                self.assertFalse((root / 'config.yaml').exists())


if __name__ == '__main__':
    unittest.main()
