"""Справка и ошибки аргументов не должны запускать конфигуратор."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_deploy import ROOT


HELP = '''anysda-vpn — интерактивный конфигуратор
Задаёт вопросы → записывает config.yaml → генерирует infra/envs/*.env

Команды:
  ./setup.sh                    Запустить мастер настройки
  ./setup.sh --front-test       Запустить тестовую панель
  ./setup.sh --front-test-down  Остановить тестовую панель
  ./setup.sh -h | --help        Показать справку
'''


class SetupHelpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copy2(ROOT / 'setup.sh', self.root / 'setup.sh')

    def run_setup(self, args, stdin):
        # Удерживаем stdin открытым: попытка прочитать его должна дать timeout.
        with subprocess.Popen(['./setup.sh', *args], cwd=self.root, stdin=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True) as process:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, 9)
                stdout, stderr = process.communicate()
                self.fail(f'Мастер ждёт stdin: {stdout}{stderr}')
            stdout, stderr = process.communicate()
            return process.returncode, stdout, stderr

    def test_help_with_open_stdin_preserves_config(self):
        for flag in ('-h', '--help'):
            for config in (None, 'invalid config: [\n'):
                with self.subTest(flag=flag, config=config):
                    path = self.root / 'config.yaml'
                    if config is None:
                        path.unlink(missing_ok=True)
                        before = None
                    else:
                        path.write_text(config)
                        path.chmod(0o640)
                        before = path.stat()
                    code, stdout, stderr = self.run_setup([flag], subprocess.PIPE)
                    self.assertEqual(code, 0, stdout + stderr)
                    self.assertEqual(stdout, HELP)
                    self.assertEqual(stderr, '')
                    if before is None:
                        self.assertFalse(path.exists())
                    else:
                        self.assertEqual(path.read_text(), config)
                        after = path.stat()
                        self.assertEqual((after.st_mode, after.st_mtime_ns, after.st_ctime_ns),
                                         (before.st_mode, before.st_mtime_ns, before.st_ctime_ns))
                    self.assertFalse((self.root / 'infra').exists())

    def test_unknown_arguments_show_error_and_help(self):
        for args in (['--unknown'], ['-x'], ['typo'], [''],
                     ['--help', '--unknown'], ['--front-test', '--unknown'],
                     ['--front-test-down', '--unknown']):
            with self.subTest(args=args):
                code, stdout, stderr = self.run_setup(args, subprocess.PIPE)
                self.assertNotEqual(code, 0)
                self.assertEqual(stdout, '')
                self.assertIn('аргумент:', stderr)
                self.assertTrue(stderr.endswith(HELP), stderr)
                self.assertNotIn('Количество', stderr)
                self.assertFalse((self.root / 'config.yaml').exists())

    def test_help_and_errors_do_not_consume_stdin(self):
        for flag in ('-h', '--help', '--unknown'):
            with self.subTest(flag=flag), tempfile.TemporaryFile() as stdin:
                stdin.write(b'input must remain unread\n')
                stdin.seek(0)
                self.run_setup([flag], stdin)
                self.assertEqual(stdin.tell(), 0)

    def test_front_test_commands_still_dispatch(self):
        script = self.root / 'app/e2e/front-test.sh'
        script.parent.mkdir(parents=True)
        script.write_text('printf "front-test %s\\n" "$*"\nexit 23\n')
        for flag, action in (('--front-test', 'up'), ('--front-test-down', 'down')):
            with self.subTest(flag=flag):
                code, stdout, stderr = self.run_setup([flag], subprocess.PIPE)
                self.assertEqual(code, 23)
                self.assertEqual(stdout, f'front-test {action}\n')
                self.assertEqual(stderr, '')
                self.assertFalse((self.root / 'config.yaml').exists())
