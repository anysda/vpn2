"""Запуск всего мастера со stdin и PTY; сетевые ответы фиксированы."""
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import termios
import time
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
    def test_bot_token_is_not_echoed_on_terminal(self):
        prompts = [
            ('Количество', '1'), ('root password', 'root secret'),
            ('IP-адрес:', '192.0.2.2'), ('root password', 'exit secret'),
            ('Тег ноды', 'se'), ('Логин', ''), ('Пароль (Enter', ''),
            ('SSH pubkey', ''), ('Настроить HTTPS', 'n'), ('Включить Telegram', 'y'),
            ('Токен бота', '123:CANARY_BOT_TOKEN'), ('Chat ID', '123'),
            ('Никнейм админа бота (@username', ''), ('Включить бэкапы?', 'n'),
            ('Вывести YouTube', 'n'), ('Сохранить config.yaml', 'y'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory)
            master, slave = pty.openpty()
            transcript = b''
            pending = b''
            token_echo = None
            with subprocess.Popen(['bash', '-c', COMMAND], cwd=directory, stdin=slave,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  start_new_session=True, env={**os.environ, 'HOME': directory}) as process:
                try:
                    for prompt, answer in prompts:
                        deadline = time.monotonic() + 5
                        while prompt.encode() not in pending:
                            if time.monotonic() > deadline:
                                self.fail(f'Не дождались вопроса: {prompt}')
                            ready, _, _ = select.select([process.stdout, master], [], [], 0.1)
                            for stream in ready:
                                chunk = os.read(stream if isinstance(stream, int) else stream.fileno(), 65536)
                                transcript += chunk
                                pending += chunk
                        pending = b''
                        # Дождаться read -s после печати вопроса (до отправки ввода).
                        time.sleep(0.05)
                        if prompt == 'Токен бота':
                            token_echo = bool(termios.tcgetattr(slave)[3] & termios.ECHO)
                        os.write(master, (answer + '\n').encode())
                    output, _ = process.communicate(timeout=5)
                    transcript += output
                    self.assertEqual(process.returncode, 0, transcript.decode())
                    self.assertFalse(token_echo, 'токен вводится при включённом ECHO')
                    self.assertNotIn(b'CANARY_BOT_TOKEN', transcript)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                    os.close(master)
                    os.close(slave)

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
