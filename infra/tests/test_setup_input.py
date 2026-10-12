"""Запуск всего мастера со stdin и PTY; сетевые ответы фиксированы."""
import importlib.util
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

import yaml

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
    shutil.copy(ROOT / 'infra/lib/hy2_bandwidth.py', root / 'infra/lib')
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
    def test_merge_preserves_example_except_answered_fields(self):
        spec = importlib.util.spec_from_file_location('config2env', ROOT / 'infra/lib/config2env.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = (ROOT / 'config.example.yaml').read_text()
        expected = yaml.safe_load(original)
        expected['entry']['password'] = 'new secret'
        expected['backup']['enabled'] = False
        result = module.merge_setup_config(original, "entry:\n  password: 'new secret'\n"
                                           'backup:\n  enabled: false\n')
        self.assertEqual(yaml.safe_load(result), expected)
        self.assertIn('# Кастомные порты (опционально).', result)

    def test_rerun_reorders_adds_and_removes_exits_by_tag(self):
        answers = ['3', 'entry secret', '192.0.2.4', 'new de password', 'de',
                   '192.0.2.3', 'new se password', 'se',
                   '192.0.2.5', 'new us password', 'us',
                   '', '', '', 'n', 'n', 'n', 'n', 'y']
        existing = '''entry:
  host: 192.0.2.1
  password: entry secret
exits:
  - host: 192.0.2.2
    tag: se
    password: old
    hy2_direct_port: 5443
  - tag: fi
    host: 192.0.2.6
    password: old
    hy2_direct_port: 6443
  - tag: de
    host: 192.0.2.7
    password: old
    hy2_direct_port: 7443
'''
        with tempfile.TemporaryDirectory() as directory:
            root = prepare(directory)
            config = root / 'config.yaml'
            config.write_text(existing)
            code, output = run_setup(directory, '\n'.join(answers) + '\n')
            self.assertEqual(code, 0, output)
            self.assertEqual(yaml.safe_load(config.read_text())['exits'], [
                {'tag': 'de', 'host': '192.0.2.4', 'password': 'new de password', 'hy2_direct_port': 7443},
                {'tag': 'se', 'host': '192.0.2.3', 'password': 'new se password', 'hy2_direct_port': 5443},
                {'tag': 'us', 'host': '192.0.2.5', 'password': 'new us password'},
            ])

    def test_rerun_disables_options_and_preserves_orchestrator_key(self):
        answers = ['1', 'entry secret', '192.0.2.2', 'exit secret', 'se',
                   '', '', '', 'n', 'n', 'n', 'n', 'y']
        existing = '''entry:
  host: 192.0.2.1
  password: old
exits:
  - tag: se
    host: 192.0.2.2
    password: old
admin:
  password: old password
  ssh_pubkey: ssh-ed25519 old
panel:
  domain: old.example.com
  image: example/panel:pinned
telegram:
  bot_token: fake-token
  chat_id: '123'
  admin_username: oldadmin
backup:
  enabled: true
  retention: 42
  local:
    dir: /srv/archives
youtube:
  route: zapret
  quic: allow
orchestrator_key: 'ZmFrZSBrZXkK' # synthetic key
orchestrator_pubkey: 'ssh-ed25519 fake' # synthetic public key
'''
        with tempfile.TemporaryDirectory() as directory:
            root = prepare(directory)
            config = root / 'config.yaml'
            config.write_text(existing)
            code, output = run_setup(directory, '\n'.join(answers) + '\n')
            self.assertEqual(code, 0, output)
            actual = yaml.safe_load(config.read_text())
            self.assertFalse(actual['backup']['enabled'])
            self.assertEqual(actual['backup']['retention'], 42)
            self.assertEqual(actual['backup']['local'], {'dir': '/srv/archives'})
            self.assertEqual(actual['youtube'], {'route': 'off', 'quic': 'allow'})
            self.assertFalse(actual['admin'].get('password'))
            self.assertFalse(actual['admin'].get('ssh_pubkey'))
            self.assertFalse(actual['panel'].get('domain'))
            self.assertEqual(actual['panel']['image'], 'example/panel:pinned')
            self.assertFalse(any(actual['telegram'].values()))
            self.assertEqual((root / '.ssh/id_ed25519').read_text(), 'fake key\n')
            self.assertIn("orchestrator_key: 'ZmFrZSBrZXkK' # synthetic key", config.read_text())
            env = (root / 'infra/envs/ru.env').read_text()
            self.assertNotIn('BACKUP_ENABLED=', env)
            self.assertNotIn('fake-token', env)
            self.assertIn("YT_ROUTE='off'", env)

    def test_rerun_rejects_invalid_yaml_without_modifying_files(self):
        answers = ['1', 'entry secret', '192.0.2.2', 'exit secret', 'se',
                   '', '', '', 'n', 'n', 'n', 'n', 'y']
        for existing in ('panel:\n  image: "CANARY unclosed\n',
                         'ports:\n  hy2_direct: 4443\n  hy2_direct: 5443\n',
                         'exits:\n  - host: 192.0.2.2\n',
                         'exits:\n  - tag: se\n  - tag: se\n'):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                root = prepare(directory)
                config = root / 'config.yaml'
                config.write_text(existing)
                env = root / 'infra/envs/ru.env'
                env.parent.mkdir()
                env.write_text('unchanged\n')
                code, output = run_setup(directory, '\n'.join(answers) + '\n')
                self.assertNotEqual(code, 0, output)
                self.assertNotIn('CANARY', output)
                self.assertEqual(config.read_text(), existing)
                self.assertEqual(env.read_text(), 'unchanged\n')
                self.assertEqual(list(root.glob('.config-*.yaml')), [])

    def test_rerun_preserves_unprompted_settings(self):
        answers = ['1', 'new entry password', '192.0.2.3', 'new exit password', 'se',
                   'newadmin', 'new password', 'new password', '', 'n', 'n',
                   'y', 'n', 'local', 'new backup password', 'y', 'y']
        existing = '''# Keep this comment
entry:
  host: 192.0.2.10
  password: old entry password
  public_host: 192.0.2.11 # public endpoint
exits:
  - tag: se
    host: 192.0.2.2
    password: old exit password
    hy2_direct_port: 5443 # selected for this exit
admin:
  user: oldadmin
  password: old password
  ssh_pubkey: ssh-ed25519 old
ports:
  hy2_direct: 4443 # keep this port
panel:
  domain: old.example.com
  image: example/panel:pinned
sso:
  label: MyProvider
  allowed_subs: 'alice,bob'
backup:
  enabled: true
  retention: 42
  local:
    dir: /srv/archives # custom directory
  s3:
    bucket: saved-bucket
youtube:
  route: direct
  quic: allow
'''
        with tempfile.TemporaryDirectory() as directory:
            root = prepare(directory)
            config = root / 'config.yaml'
            config.write_text(existing)
            for _ in range(2):
                code, output = run_setup(directory, '\n'.join(answers) + '\n')
                self.assertEqual(code, 0, output)
                actual = yaml.safe_load(config.read_text())
                self.assertEqual(actual.get('ports'), {'hy2_direct': 4443})
                self.assertEqual(actual['panel']['image'], 'example/panel:pinned')
                self.assertEqual(actual['sso'], {'label': 'MyProvider', 'allowed_subs': 'alice,bob'})
                self.assertEqual(actual['backup']['retention'], 42)
                self.assertEqual(actual['backup']['local'], {'dir': '/srv/archives'})
                self.assertEqual(actual['backup']['s3'], {'bucket': 'saved-bucket'})
                self.assertEqual(actual['youtube'], {'route': 'zapret', 'quic': 'allow'})
                self.assertEqual(actual['exits'], [{'tag': 'se', 'host': '192.0.2.3',
                                                  'password': 'new exit password',
                                                  'hy2_direct_port': 5443}])
                self.assertEqual(actual['entry'], {'host': '192.0.2.1',
                                                  'password': 'new entry password',
                                                  'public_host': '192.0.2.11'})
                self.assertEqual(actual['admin']['user'], 'newadmin')
                self.assertEqual(actual['admin']['password'], 'new password')
                self.assertFalse(actual['admin'].get('ssh_pubkey'))
                self.assertFalse(actual['panel'].get('domain'))
                self.assertEqual(actual['backup']['passphrase'], 'new backup password')
                self.assertEqual(actual['backup']['schedule'], 'off')
                for comment in ('# Keep this comment', '# public endpoint', '# keep this port',
                                '# selected for this exit', '# custom directory'):
                    self.assertIn(comment, config.read_text())
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_exit_tag_syntax(self):
        body = (ROOT / 'setup.sh').read_text().split('validate_tag() {', 1)[1].split('\n}', 1)[0]
        script = 'fail() { echo "$*" >&2; }\nvalidate_tag() {' + body + '\n}\nvalidate_tag "$1" "$2"'
        for tag in ('a', 'us2', 'us-2', 'us--2', '-us', 'us-', 'US', 'us_2',
                    'ru', 'all', 'exits', 'foreign'):
            with self.subTest(tag=tag):
                result = subprocess.run(['bash', '-euc', script, 'tag-test', tag, ''],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode == 0,
                                 tag in ('a', 'us2', 'us-2', 'us--2'), result.stderr)
        result = subprocess.run(['bash', '-euc', script, 'tag-test', 'us-2', 'se us-2 us2'],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('уже используется', result.stderr)

    def test_hyphenated_tag_through_wizard(self):
        answers = ['1', 'root secret', '192.0.2.2', 'exit secret', 'us-2',
                   '', '', '', 'n', 'n', 'n', 'n', 'y']
        with tempfile.TemporaryDirectory() as directory:
            root = prepare(directory)
            code, output = run_setup(directory, '\n'.join(answers) + '\n')
            self.assertEqual(code, 0, output)
            self.assertIn('Выход us-2: 192.0.2.2', output)
            self.assertTrue((root / 'infra/envs/us-2.env').is_file())

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
