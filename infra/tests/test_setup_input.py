"""Запуск всего мастера со stdin и PTY; сетевые ответы фиксированы."""
import difflib
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
from vpn2 import config as vpn2_config, nodevars, setup


STUBS = {
    'ip': "printf 'default via 192.0.2.254 dev eth0\\n    inet 192.0.2.1/24 scope global eth0\\n'",
    'curl': "printf '{}'",
    'dig': "printf '192.0.2.1\\n'",
}
COMMAND = 'PATH="$PWD/stubs:$PATH" exec bash setup.sh'
COPIED = ('setup.sh', 'infra/ansible/inventory.py',
          'infra/vpn2/src/vpn2/__init__.py', 'infra/vpn2/src/vpn2/setup.py', 'infra/vpn2/src/vpn2/config.py',
          'infra/vpn2/src/vpn2/nodevars.py', 'infra/vpn2/src/vpn2/watchdog/__init__.py',
          'infra/vpn2/src/vpn2/watchdog/hy2_bandwidth.py')


def node_env(root, node):
    cfg = vpn2_config.loads((root / 'config.yaml').read_text())
    return nodevars.env_by_node(cfg, state=root / 'state', repo_root=root)[node]


def prepare(directory):
    root = Path(directory)
    for name in COPIED:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, root / name)
    (root / 'stubs').mkdir()
    for name, body in STUBS.items():
        (root / 'stubs' / name).write_text(f'#!/bin/sh\n{body}\n')
        (root / 'stubs' / name).chmod(0o755)
    return root


def run_setup(directory, answers):
    with subprocess.Popen(['bash', '-c', COMMAND], cwd=directory,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, start_new_session=True, env={**os.environ, 'HOME': directory, 'VPN2_STATE_DIR': f'{directory}/state'}) as process:
        try:
            output, _ = process.communicate(answers, timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise AssertionError('мастер не завершился после EOF') from None
        return process.returncode, output


class SetupInputTests(unittest.TestCase):
    def test_invalid_ip_is_retried_before_saving(self):
        answers = ['1', 'entry secret', '999.2.3.4', '192.0.2.999', '192.0.02.2',
                   '', '192.0.2.2', 'exit secret', 'se', '', '', '', 'n', 'n', 'n', 'n', 'y']
        with tempfile.TemporaryDirectory() as directory:
            root = prepare(directory)
            code, output = run_setup(directory, '\n'.join(answers) + '\n')
            self.assertEqual(code, 0, output)
            config = yaml.safe_load((root / 'config.yaml').read_text())
            self.assertEqual(config['exits'][0]['host'], '192.0.2.2')
            self.assertEqual(output.count('Некорректный IP-адрес:'), 4)
            self.assertNotIn('ERROR: config.yaml:', output)

    def test_optional_domain_empty_or_invalid_then_empty(self):
        for domains in ([''], ['   '], ['https://vpn.example.com', ''],
                        ['vpn.example.com:443', ''], ['bad domain', '']):
            with self.subTest(domains=domains), tempfile.TemporaryDirectory() as directory:
                root = prepare(directory)
                (root / 'config.yaml').write_text('panel:\n  domain: old.example.test\n')
                answers = ['1', 'entry secret', '192.0.2.2', 'exit secret', 'se',
                           '', '', '', 'y', *domains, 'n', 'n', 'n', 'y']
                code, output = run_setup(directory, '\n'.join(answers) + '\n')
                self.assertEqual(code, 0, output)
                self.assertFalse(yaml.safe_load((root / 'config.yaml').read_text())['panel']['domain'])
                self.assertIn('Enter — без домена', output)
                self.assertNotIn('ERROR: config.yaml:', output)
                self.assertNotIn('проверяю DNS', output)
                self.assertEqual(output.count('Неправильный формат домена'), len(domains) - 1)

    def test_optional_answers_accept_empty_and_defaults(self):
        answers = ['1', 'entry secret', '192.0.2.2', 'exit secret', 'se',
                   '', '', '', '', 'y', '123:test-token', '123', '',
                   '', '', '', '', '', 'y']
        with tempfile.TemporaryDirectory() as directory:
            root = prepare(directory)
            code, output = run_setup(directory, '\n'.join(answers) + '\n')
            self.assertEqual(code, 0, output)
            config = yaml.safe_load((root / 'config.yaml').read_text())
            self.assertEqual(config['admin']['user'], 'admin')
            self.assertFalse(config['admin']['password'])
            self.assertFalse(config['admin']['ssh_pubkey'])
            self.assertFalse(config['panel']['domain'])
            self.assertFalse(config['telegram']['admin_username'])
            self.assertEqual(config['backup']['backend'], 'local')
            self.assertEqual(config['backup']['schedule'], 'daily')
            self.assertEqual(len(config['backup']['passphrase']), 40)
            self.assertNotIn('ERROR: config.yaml:', output)

    def test_merge_keeps_inline_comments_outside_yaml_quotes(self):
        for value in ("'old # secret'", "'it''s # secret'", '"escaped \\" # secret"',
                      'plain#secret', "''", ''):
            with self.subTest(value=value):
                original = f'admin:\n  password: {value}  # keep credential note\n'
                result = setup.update(original, {'admin': {'password': "'new # secret'"}})
                self.assertEqual(yaml.safe_load(result)['admin']['password'], 'new # secret')
                self.assertIn('# keep credential note', result)

    def test_merge_preserves_example_except_answered_fields(self):
        original = (ROOT / 'config.example.yaml').read_text()
        expected = yaml.safe_load(original)
        expected['entry']['password'] = 'new secret'
        expected['backup']['enabled'] = False
        result = setup.update(original, {'entry': {'password': "'new secret'"}, 'backup': {'enabled': 'false'}})
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
        # Пароль админа на Enter остаётся прежним, ключ убирает явный «-».
        answers = ['1', 'entry secret', '192.0.2.2', 'exit secret', 'se',
                   '', '', '-', 'n', 'n', 'n', 'n', 'y']
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
            self.assertEqual(actual['admin'], {'password': 'old password', 'ssh_pubkey': ''})
            self.assertEqual(actual['entry']['password'], 'entry secret')
            self.assertEqual(actual['exits'][0]['password'], 'exit secret')
            self.assertFalse(actual['panel'].get('domain'))
            self.assertEqual(actual['panel']['image'], 'example/panel:pinned')
            self.assertFalse(any(actual['telegram'].values()))
            self.assertEqual((root / '.ssh/id_ed25519').read_text(), 'fake key\n')
            self.assertIn("orchestrator_key: 'ZmFrZSBrZXkK' # synthetic key", config.read_text())
            env = node_env(root, 'ru')
            self.assertNotIn('BACKUP_ENABLED', env)
            self.assertNotIn('fake-token', env.values())
            self.assertEqual(env['YT_ROUTE'], 'off')

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
                code, output = run_setup(directory, '\n'.join(answers) + '\n')
                self.assertNotEqual(code, 0, output)
                self.assertNotIn('CANARY', output)
                self.assertEqual(config.read_text(), existing)
                self.assertEqual(list(root.glob('.config-*.yaml')), [])

    def test_rerun_preserves_unprompted_settings(self):
        # Первый проход: адрес entry подтверждается (записан не тот, что определился),
        # ключ админа убирается явным «-». Второй проход повторяет те же ответы.
        answers = [['1', '192.0.2.1', 'new entry password', '192.0.2.3', 'new exit password', 'se',
                    'newadmin', 'new password', 'new password', '-', 'n', 'n',
                    'y', 'n', 'local', 'new backup password', 'y', 'y'],
                   ['1', 'new entry password', '192.0.2.3', 'new exit password', 'se',
                    'newadmin', 'new password', 'new password', '', 'n', 'n',
                    'y', 'n', 'local', 'new backup password', 'y', 'y']]
        existing = '''# Keep this comment
entry:
  host: 192.0.2.10
  password: old entry password # entry credential
  public_host: 192.0.2.11 # public endpoint
exits:
  - tag: se
    host: 192.0.2.2
    password: 'old # exit password' # exit credential
    hy2_direct_port: 5443 # selected for this exit
admin:
  user: "oldadmin" # panel login
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
            for attempt in range(2):
                code, output = run_setup(directory, '\n'.join(answers[attempt]) + '\n')
                self.assertEqual(code, 0, output)
                if attempt == 0:
                    print(''.join(difflib.unified_diff(existing.splitlines(True),
                                                     config.read_text().splitlines(True),
                                                     fromfile='config.yaml before', tofile='config.yaml after')))
                actual = yaml.safe_load(config.read_text())
                self.assertEqual(actual.get('ports'), {'hy2_direct': 4443})
                self.assertEqual(actual['panel']['image'], 'example/panel:pinned')
                self.assertEqual(actual['sso'], {'label': 'MyProvider', 'allowed_subs': 'alice,bob'})
                self.assertEqual(actual['backup']['retention'], 42)
                self.assertEqual(actual['backup']['local'], {'dir': '/srv/archives'})
                self.assertEqual(actual['backup']['s3'], {'bucket': 'saved-bucket'})
                self.assertEqual(actual['youtube'], {'route': 'direct', 'quic': 'allow'})
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
                                '# selected for this exit', '# custom directory',
                                '# entry credential', '# exit credential', '# panel login'):
                    self.assertIn(comment, config.read_text())
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                self.assertIn('обновляет только спрошенные поля', output)
                self.assertIn('остальные настройки и комментарии в config.yaml сохраняются', output)

    def test_hyphenated_tag_through_wizard(self):
        answers = ['1', 'root secret', '192.0.2.2', 'exit secret', 'us-2',
                   '', '', '', 'n', 'n', 'n', 'n', 'y']
        with tempfile.TemporaryDirectory() as directory:
            root = prepare(directory)
            code, output = run_setup(directory, '\n'.join(answers) + '\n')
            self.assertEqual(code, 0, output)
            self.assertIn('Выход us-2: 192.0.2.2', output)
            self.assertEqual(node_env(root, 'us-2')['SSH_HOST'], '192.0.2.2')

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
                                  start_new_session=True, env={**os.environ, 'HOME': directory, 'VPN2_STATE_DIR': f'{directory}/state'}) as process:
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
            ru, se = node_env(root, 'ru'), node_env(root, 'se')
            self.assertEqual([ru['SSH_PASS'], ru['ADMIN_PASSWORD'], ru['BACKUP_PASSPHRASE'], se['SSH_PASS']], [secret] * 4)
            self.assertNotIn('CANARY', output)
            self.assertEqual((root / 'config.yaml').stat().st_mode & 0o777, 0o600)

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
