"""Мастер setup.sh целиком со stdin и PTY: повторы ввода, слияние с прежним config.yaml, секреты, справка.

ip, curl и dig отвечают всегда одно и то же: шлюз 192.0.2.254, адрес 192.0.2.1, пустой JSON, A-запись 192.0.2.1.
"""
import os
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import termios
import time

import pytest
import yaml

from setup_support import ROOT, environment, node_env, prepare, run
from vpn2 import setup

STUBS = {'ip': "printf 'default via 192.0.2.254 dev eth0\\n    inet 192.0.2.1/24 scope global eth0\\n'",
         'curl': "printf '{}'", 'dig': "printf '192.0.2.1\\n'"}
ONE_EXIT = ['1', 'entry secret', '192.0.2.2', 'exit secret', 'se']
HELP = '''anysda-vpn — интерактивный конфигуратор
Задаёт вопросы → записывает config.yaml

Команды:
  ./setup.sh                    Запустить мастер настройки
  ./setup.sh -h | --help        Показать справку
'''


def repo(tmp_path, existing=None):
    root = prepare(tmp_path)
    for name, body in STUBS.items():
        (tmp_path / 'bin' / name).write_text(f'#!/bin/sh\n{body}\n')
    if existing is not None:
        (root / 'config.yaml').write_text(existing)
    return root


def wizard(tmp_path, answers, existing=None):
    """(корень копии, код, вывод) прохода мастера с ответами по строкам."""
    root = repo(tmp_path, existing)
    code, output = run(root, '\n'.join(answers) + '\n' if isinstance(answers, list) else answers, timeout=10)
    return root, code, output


def saved(root):
    return yaml.safe_load((root / 'config.yaml').read_text())


def test_invalid_ip_is_retried_before_saving(tmp_path):
    root, code, output = wizard(tmp_path, ['1', 'entry secret', '999.2.3.4', '192.0.2.999', '192.0.02.2', '',
                                           '192.0.2.2', 'exit secret', 'se', '', '', '', 'n', 'n', 'n', 'n', 'y'])
    assert code == 0, output
    assert saved(root)['exits'][0]['host'] == '192.0.2.2'
    assert output.count('Некорректный IP-адрес:') == 4
    assert 'ERROR: config.yaml:' not in output


@pytest.mark.parametrize('domains', [[''], ['   '], ['https://vpn.example.com', ''], ['vpn.example.com:443', ''],
                                     ['bad domain', '']])
def test_optional_domain_empty_or_invalid_then_empty(tmp_path, domains):
    root, code, output = wizard(tmp_path, [*ONE_EXIT, '', '', '', 'y', *domains, 'n', 'n', 'n', 'y'],
                                'panel:\n  domain: old.example.test\n')
    assert code == 0, output
    assert not saved(root)['panel']['domain']
    assert 'Enter — без домена' in output
    assert 'ERROR: config.yaml:' not in output and 'проверяю DNS' not in output
    assert output.count('Неправильный формат домена') == len(domains) - 1


def test_wizard_accepts_idn(tmp_path):
    root, code, output = wizard(tmp_path, ['1', 'root secret', '192.0.2.2', 'exit secret', 'se', '', '', '',
                                           'y', 'ПРИМЕР.РФ', 'n', 'n', 'n', 'y'])
    assert code == 0, output
    assert 'xn--e1afmkfd.xn--p1ai' in (root / 'config.yaml').read_text()


def test_optional_answers_accept_empty_and_defaults(tmp_path):
    root, code, output = wizard(tmp_path, [*ONE_EXIT, '', '', '', '', 'y', '123:test-token', '123', '',
                                           '', '', '', '', '', 'y'])
    assert code == 0, output
    config = saved(root)
    assert config['admin']['user'] == 'admin'
    assert not any((config['admin']['password'], config['admin']['ssh_pubkey'], config['panel']['domain'],
                    config['telegram']['admin_username']))
    assert (config['backup']['backend'], config['backup']['schedule']) == ('local', 'daily')
    assert len(config['backup']['passphrase']) == 40
    assert 'ERROR: config.yaml:' not in output


@pytest.mark.parametrize('value', ["'old # secret'", "'it''s # secret'", '"escaped \\" # secret"', 'plain#secret',
                                   "''", ''])
def test_merge_keeps_inline_comments_outside_yaml_quotes(value):
    result = setup.update(f'admin:\n  password: {value}  # keep credential note\n',
                          {'admin': {'password': "'new # secret'"}})
    assert yaml.safe_load(result)['admin']['password'] == 'new # secret'
    assert '# keep credential note' in result


def test_merge_preserves_example_except_answered_fields():
    original = (ROOT / 'config.example.yaml').read_text()
    expected = yaml.safe_load(original)
    expected['entry']['password'], expected['backup']['enabled'] = 'new secret', False
    result = setup.update(original, {'entry': {'password': "'new secret'"}, 'backup': {'enabled': 'false'}})
    assert yaml.safe_load(result) == expected
    assert '# Кастомные порты (опционально).' in result


def test_rerun_reorders_adds_and_removes_exits_by_tag(tmp_path):
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
    root, code, output = wizard(tmp_path, ['3', 'entry secret', '192.0.2.4', 'new de password', 'de', '192.0.2.3',
                                           'new se password', 'se', '192.0.2.5', 'new us password', 'us',
                                           '', '', '', 'n', 'n', 'n', 'n', 'y'], existing)
    assert code == 0, output
    assert saved(root)['exits'] == [
        {'tag': 'de', 'host': '192.0.2.4', 'password': 'new de password', 'hy2_direct_port': 7443},
        {'tag': 'se', 'host': '192.0.2.3', 'password': 'new se password', 'hy2_direct_port': 5443},
        {'tag': 'us', 'host': '192.0.2.5', 'password': 'new us password'}]


def test_rerun_disables_options_and_preserves_orchestrator_key(tmp_path):
    # Пароль админа на Enter остаётся прежним, ключ убирает явный «-».
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
    root, code, output = wizard(tmp_path, [*ONE_EXIT, '', '', '-', 'n', 'n', 'n', 'n', 'y'], existing)
    assert code == 0, output
    actual = saved(root)
    assert not actual['backup']['enabled']
    assert (actual['backup']['retention'], actual['backup']['local']) == (42, {'dir': '/srv/archives'})
    assert actual['youtube'] == {'route': 'off', 'quic': 'allow'}
    assert actual['admin'] == {'password': 'old password', 'ssh_pubkey': ''}
    assert (actual['entry']['password'], actual['exits'][0]['password']) == ('entry secret', 'exit secret')
    assert not actual['panel'].get('domain') and actual['panel']['image'] == 'example/panel:pinned'
    assert not any(actual['telegram'].values())
    assert (tmp_path / 'home/.ssh/id_ed25519').read_text() == 'fake key\n'
    assert "orchestrator_key: 'ZmFrZSBrZXkK' # synthetic key" in (root / 'config.yaml').read_text()
    env = node_env(root)
    assert 'BACKUP_ENABLED' not in env and 'fake-token' not in env.values()
    assert env['YT_ROUTE'] == 'off'


@pytest.mark.parametrize('existing', ['panel:\n  image: "CANARY unclosed\n', 'ports:\n  hy2_direct: 4443\n  hy2_direct: 5443\n',
                                      'exits:\n  - host: 192.0.2.2\n', 'exits:\n  - tag: se\n  - tag: se\n'])
def test_rerun_rejects_invalid_yaml_without_modifying_files(tmp_path, existing):
    root, code, output = wizard(tmp_path, [*ONE_EXIT, '', '', '', 'n', 'n', 'n', 'n', 'y'], existing)
    assert code != 0, output
    assert 'CANARY' not in output
    assert (root / 'config.yaml').read_text() == existing
    assert list(root.glob('.config-*.yaml')) == []


def test_rerun_preserves_unprompted_settings(tmp_path):
    # Первый проход подтверждает адрес entry (записан не тот, что определился) и убирает ключ админа «-»;
    # второй повторяет те же ответы на Enter.
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
    tail = ['newadmin', 'new password', 'new password']
    rest = ['n', 'n', 'y', 'n', 'local', 'new backup password', 'y', 'y']
    root = repo(tmp_path, existing)
    for answers in (['1', '192.0.2.1', 'new entry password', '192.0.2.3', 'new exit password', 'se', *tail, '-', *rest],
                    ['1', 'new entry password', '192.0.2.3', 'new exit password', 'se', *tail, '', *rest]):
        code, output = run(root, '\n'.join(answers) + '\n', timeout=10)
        assert code == 0, output
        actual, text = saved(root), (root / 'config.yaml').read_text()
        assert actual.get('ports') == {'hy2_direct': 4443}
        assert actual['panel']['image'] == 'example/panel:pinned'
        assert not actual['panel'].get('domain')
        assert actual['sso'] == {'label': 'MyProvider', 'allowed_subs': 'alice,bob'}
        assert (actual['backup']['retention'], actual['backup']['local'], actual['backup']['s3']) == (
            42, {'dir': '/srv/archives'}, {'bucket': 'saved-bucket'})
        assert (actual['backup']['passphrase'], actual['backup']['schedule']) == ('new backup password', 'off')
        assert actual['youtube'] == {'route': 'direct', 'quic': 'allow'}
        assert actual['exits'] == [{'tag': 'se', 'host': '192.0.2.3', 'password': 'new exit password',
                                    'hy2_direct_port': 5443}]
        assert actual['entry'] == {'host': '192.0.2.1', 'password': 'new entry password', 'public_host': '192.0.2.11'}
        assert (actual['admin']['user'], actual['admin']['password']) == ('newadmin', 'new password')
        assert not actual['admin'].get('ssh_pubkey')
        for comment in ('# Keep this comment', '# public endpoint', '# keep this port', '# selected for this exit',
                        '# custom directory', '# entry credential', '# exit credential', '# panel login'):
            assert comment in text
        assert (root / 'config.yaml').stat().st_mode & 0o777 == 0o600
        assert 'обновляет только спрошенные поля' in output
        assert 'остальные настройки и комментарии в config.yaml сохраняются' in output


def test_hyphenated_tag_through_wizard(tmp_path):
    root, code, output = wizard(tmp_path, ['1', 'root secret', '192.0.2.2', 'exit secret', 'us-2', '', '', '',
                                           'n', 'n', 'n', 'n', 'y'])
    assert code == 0, output
    assert 'Выход us-2: 192.0.2.2' in output
    assert node_env(root, 'us-2')['SSH_HOST'] == '192.0.2.2'


def test_bot_token_is_not_echoed_on_terminal(tmp_path):
    prompts = [('Количество', '1'), ('root password', 'root secret'), ('IP-адрес:', '192.0.2.2'),
               ('root password', 'exit secret'), ('Тег ноды', 'se'), ('Логин', ''), ('Пароль (Enter', ''),
               ('SSH pubkey', ''), ('Настроить HTTPS', 'n'), ('Включить Telegram', 'y'),
               ('Токен бота', '123:CANARY_BOT_TOKEN'), ('Chat ID', '123'), ('Никнейм админа бота (@username', ''),
               ('Включить бэкапы?', 'n'), ('Вывести YouTube', 'n'), ('Сохранить config.yaml', 'y')]
    root = repo(tmp_path)
    master, slave = pty.openpty()
    transcript, token_echo = b'', None
    with subprocess.Popen(['bash', str(root / 'setup.sh')], cwd=root, env=environment(root), stdin=slave,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True) as process:
        try:
            for prompt, answer in prompts:
                pending, deadline = b'', time.monotonic() + 5
                while prompt.encode() not in pending:
                    assert time.monotonic() < deadline, f'не дождались вопроса: {prompt}'
                    for stream in select.select([process.stdout, master], [], [], 0.1)[0]:
                        chunk = os.read(stream if isinstance(stream, int) else stream.fileno(), 65536)
                        transcript, pending = transcript + chunk, pending + chunk
                if prompt == 'Токен бота':
                    # Вопрос печатается до того, как ввод гасит ECHO: ждём само гашение.
                    deadline = time.monotonic() + 5
                    while (token_echo := bool(termios.tcgetattr(slave)[3] & termios.ECHO)) and \
                            time.monotonic() < deadline:
                        time.sleep(0.01)
                os.write(master, (answer + '\n').encode())
            transcript += process.communicate(timeout=5)[0]
            assert process.returncode == 0, transcript.decode()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            os.close(master)
            os.close(slave)
    assert token_echo is False, 'токен вводится при включённом ECHO'
    assert b'CANARY_BOT_TOKEN' not in transcript


def test_password_whitespace_survives_stdin_and_env(tmp_path):
    secret = '  CANARY \'" $HOME `true` \\ tail\t '
    root, code, output = wizard(tmp_path, ['1', secret, '192.0.2.2', secret, 'se', '', secret, secret, '', 'n', 'n',
                                           'y', 'n', 'local', secret, 'n', 'y'])
    assert code == 0, output
    ru, se = node_env(root), node_env(root, 'se')
    assert [ru['SSH_PASS'], ru['ADMIN_PASSWORD'], ru['BACKUP_PASSPHRASE'], se['SSH_PASS']] == [secret] * 4
    assert 'CANARY' not in output
    assert (root / 'config.yaml').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('answers', ['', '1\n', '1\nroot secret\n'])
def test_eof_aborts_without_writing_config(tmp_path, answers):
    root, code, output = wizard(tmp_path, answers)
    assert code != 0, output
    assert 'EOF' in output
    assert not (root / 'config.yaml').exists()


def help_run(root, args, stdin=subprocess.PIPE):
    """(код, stdout, stderr) справки или ошибки аргументов; stdin открыт, попытка его читать - зависание."""
    with subprocess.Popen(['bash', str(root / 'setup.sh'), *args], cwd=root, stdin=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            pytest.fail('мастер ждёт stdin: ' + ''.join(process.communicate()))
        return process.returncode, *process.communicate()


@pytest.fixture
def bare(tmp_path):
    """Каталог с одним setup.sh: справке не нужно ничего другого."""
    shutil.copy2(ROOT / 'setup.sh', tmp_path / 'setup.sh')
    return tmp_path


@pytest.mark.parametrize('flag', ['-h', '--help'])
@pytest.mark.parametrize('config', [None, 'invalid config: [\n'])
def test_help_with_open_stdin_preserves_config(bare, flag, config):
    path = bare / 'config.yaml'
    if config is not None:
        path.write_text(config)
        path.chmod(0o640)
        before = path.stat()
    assert help_run(bare, [flag]) == (0, HELP, '')
    if config is None:
        assert not path.exists()
    else:
        after = path.stat()
        assert path.read_text() == config
        assert (after.st_mode, after.st_mtime_ns, after.st_ctime_ns) == (before.st_mode, before.st_mtime_ns,
                                                                         before.st_ctime_ns)
    assert not (bare / 'infra').exists()


@pytest.mark.parametrize('args', [['--unknown'], ['-x'], ['typo'], [''], ['--help', '--unknown'], ['--front-test'],
                                  ['--front-test-down']])
def test_unknown_arguments_show_error_and_help(bare, args):
    code, stdout, stderr = help_run(bare, args)
    assert (code != 0, stdout) == (True, '')
    assert 'аргумент:' in stderr and stderr.endswith(HELP) and 'Количество' not in stderr
    assert not (bare / 'config.yaml').exists()


@pytest.mark.parametrize('flag', ['-h', '--help', '--unknown'])
def test_help_and_errors_do_not_consume_stdin(bare, flag):
    with tempfile.TemporaryFile() as stdin:
        stdin.write(b'input must remain unread\n')
        stdin.seek(0)
        help_run(bare, [flag], stdin)
        assert stdin.tell() == 0
