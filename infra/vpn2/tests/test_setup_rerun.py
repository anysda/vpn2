"""Повторный запуск мастера при существующем config.yaml.

Enter на вопросе оставляет прежний ответ, секреты сами не меняются, а повтор
на одних Enter не меняет config.yaml ни на байт. Идёт и на Python 3.11.
"""
import os
import pty
import signal
import subprocess
import termios
import time

import pytest

from setup_support import FIXTURES, environment, prepare, run
from test_setup import wait_for

STUBS = {'ipify': '192.0.2.1', 'geo-198.51.100.2': '{}', 'geo-198.51.100.3': '{}',
         'dig-vpn.example.com': '192.0.2.1\n'}
ENTER = '\n' * 60
PASSPHRASE = 'Zx9' * 13 + 'Q'
FIRST = ['2', 'entry pw', '198.51.100.2', 'exit pw 1', 'de', '198.51.100.3', 'exit pw 2', 'se',
         'boss', 'OwnAdminPw-1', 'OwnAdminPw-1', 'ssh-ed25519 AAAAC3Nza me@host',
         'y', 'vpn.example.com', 'y', '123456:FAKE-token', '4242', '@boss',
         'y', 'y', 'local', '', 'y', 'y']
SECRETS = ('entry pw', 'exit pw 1', 'exit pw 2', 'OwnAdminPw-1', '123456:FAKE-token')
# Запись руками, как в config.example.yaml: без кавычек, с комментариями и
# настройками, о которых мастер не спрашивает.
HAND = '''# Keep this comment
entry:
  host: 192.0.2.10
  password: old entry password # entry credential
  public_host: 192.0.2.11 # public endpoint
exits:
  - tag: se
    host: 192.0.2.2
    password: 'old # exit password' # exit credential
    hy2_direct_port: 5443 # selected for this exit
  - host: 192.0.2.3
    tag: fi
    password: "old fi"
admin:
  user: "oldadmin" # panel login
  password: old password
  ssh_pubkey: ssh-ed25519 old
ports:
  hy2_direct: 4443 # keep this port
panel:
  domain: old.example.com
  image: example/panel:pinned
telegram:
  bot_token: 1:old-token
  chat_id: -100
  admin_username: oldnick
backup:
  enabled: yes
  retention: 42
  passphrase: old backup passphrase
  schedule: off
  backend: s3
  s3:
    endpoint: https://s3.example.com
    bucket: saved-bucket
    access_key: AK
    secret_key: old s3 secret
youtube:
  route: direct
  quic: allow
orchestrator_pubkey: 'ssh-ed25519 fake' # synthetic public key
'''
HAND_SECRETS = ('old entry password', 'exit password', 'old fi', 'old password', '1:old-token',
                'old backup passphrase', 'old s3 secret')


def lines(*answers):
    return '\n'.join(answers) + '\n'


def filled(tmp_path, text=None, stubs=STUBS):
    """Копия репы с готовым config.yaml: text или итог первого запуска."""
    root = prepare(tmp_path, stubs=stubs)
    config = root / 'config.yaml'
    if text is None:
        code, output = run(root, lines(*FIRST))
        assert code == 0, output
    else:
        config.write_text(text)
        config.chmod(0o600)
    return root, config


def rerun(root, answers=ENTER):
    code, output = run(root, answers)
    assert 'Traceback' not in output
    return code, output


def test_enter_on_every_question_keeps_config(tmp_path):
    root, config = filled(tmp_path)
    before = config.read_bytes()
    assert b"passphrase: '" in before and b"domain: 'vpn.example.com'" in before
    envs = {path.name: path.read_bytes() for path in (root / 'infra/envs').iterdir()}
    code, output = rerun(root)
    assert code == 0, output
    assert config.read_bytes() == before
    assert config.stat().st_mode & 0o777 == 0o600
    assert {path.name: path.read_bytes() for path in (root / 'infra/envs').iterdir()} == envs
    passphrase = before.decode().split("passphrase: '")[1].split("'")[0]
    for secret in (*SECRETS, passphrase):
        assert secret not in output
    assert 'Passphrase сгенерирован' not in output
    # Прежние ответы показаны как значения по умолчанию.
    for shown in ('Количество [2]', 'IP-адрес [198.51.100.2]', 'Тег ноды [se]', 'Логин [boss]',
                  'Настроить HTTPS-домен для панели? (y/n) [y]', '[vpn.example.com]',
                  'Включить Telegram-бот? (y/n) [y]', 'Chat ID (число из @userinfobot) [4242]',
                  'Включить бэкапы? (y/n) [y]', 'Backend (local/s3) [local]'):
        assert shown in output, shown
    assert 'config.yaml без изменений' in output


@pytest.mark.parametrize('name', ['branches', 'colors', 'full', 'manual-entry', 'minimal'])
@pytest.mark.parametrize('detected', [True, False])
def test_enter_keeps_config_of_previous_release(tmp_path, name, detected):
    """expected.yaml записаны с bash-мастера прошлого выпуска."""
    stubs = FIXTURES / name / 'stubs'
    answers = {path.name: path.read_text() for path in stubs.iterdir()} if stubs.is_dir() else {}
    if not detected:   # адрес entry определился иначе, чем записан: прежний остаётся
        answers = {'ipify': '203.0.113.99'}
    text = (FIXTURES / name / 'expected.yaml').read_text().replace('<GENERATED>', PASSPHRASE)
    root, config = filled(tmp_path, text, answers)
    code, output = rerun(root)
    assert code == 0, output
    assert config.read_text() == text
    assert PASSPHRASE not in output


def test_enter_keeps_handwritten_config(tmp_path):
    root, config = filled(tmp_path, HAND)
    code, output = rerun(root)
    assert code == 0, output
    assert config.read_text() == HAND
    for secret in HAND_SECRETS:
        assert secret not in output
    assert 'Автоопределение даёт 192.0.2.1, в config.yaml записан 192.0.2.10' in output
    assert 'IP-адрес входной ноды [192.0.2.10]' in output
    assert 'S3 bucket [saved-bucket]' in output
    assert 'Ежедневный бэкап с 02:00 (TZ entry, случайная задержка до 5 мин)? (y/n) [n]' in output
    env = (root / 'infra/envs/ru.env').read_text()
    assert "BACKUP_PASSPHRASE='old backup passphrase'" in env and "YT_ROUTE='direct'" in env


CLEARED = '''entry:
  host: '192.0.2.1'
  password: 'entry pw'
exits:
  - tag: de
    host: '198.51.100.2'
    password: 'exit pw'
admin:
  user: ''
  password: ''
  ssh_pubkey: ''
panel:
  domain: ''
telegram:
  bot_token: ''
  chat_id: '4242'
backup:
  enabled: false
  passphrase: 'kept while disabled'
youtube:
  route: ''
'''


def test_enter_keeps_fields_cleared_by_operator(tmp_path):
    root, config = filled(tmp_path, CLEARED)
    code, output = rerun(root)
    assert code == 0, output
    assert config.read_text() == CLEARED
    for shown in ('Логин [anysda]', 'Настроить HTTPS-домен для панели? (y/n) [n]',
                  'Включить Telegram-бот? (y/n) [n]', 'Включить бэкапы? (y/n) [n]',
                  'Вывести YouTube на РФ-выход entry с обходом DPI? (y/n) [n]'):
        assert shown in output, shown
    assert 'Passphrase сгенерирован' not in output


def test_missing_passphrase_is_generated_once_and_reported(tmp_path):
    text = CLEARED.replace("enabled: false\n  passphrase: 'kept while disabled'", 'enabled: true')
    root, config = filled(tmp_path, text)
    code, output = rerun(root)
    assert code == 0, output
    assert 'Passphrase сгенерирован; после сохранения он будет в config.yaml (chmod 600).' in output
    after = config.read_text()
    passphrase = after.split("passphrase: '")[1].split("'")[0]
    assert len(passphrase) == 40 and passphrase.isalnum() and passphrase not in output
    assert after.replace(f"  passphrase: '{passphrase}'\n", '') == text
    code, output = rerun(root)
    assert code == 0, output
    assert config.read_text() == after
    assert 'Passphrase сгенерирован' not in output


def test_reenabled_backup_keeps_stored_passphrase(tmp_path):
    root, config = filled(tmp_path, CLEARED)
    code, output = rerun(root, '\n' * 10 + 'y\n' + ENTER)
    assert code == 0, output
    assert config.read_text() == CLEARED.replace('enabled: false', 'enabled: true')
    assert 'Passphrase сгенерирован' not in output and 'kept while disabled' not in output


@pytest.mark.parametrize('answers', ['', '\n\n\n', '\n' * 12])
def test_input_end_keeps_filled_config(tmp_path, answers):
    """stdin не терминал и кончился раньше вопросов: код 1, файл прежний."""
    root, config = filled(tmp_path)
    before = config.read_bytes()
    code, output = rerun(root, answers)
    assert code == 1, output
    assert 'ввод прерван' in output or answers
    assert config.read_bytes() == before
    assert [path.name for path in root.glob('.config-*')] == []


@pytest.mark.parametrize('number', [signal.SIGINT, signal.SIGTERM])
def test_signal_in_the_middle_keeps_filled_config(tmp_path, number):
    root, config = filled(tmp_path)
    before = config.read_bytes()
    master, slave = pty.openpty()
    try:
        with subprocess.Popen(['bash', str(root / 'setup.sh')], cwd=root, env=environment(root),
                              stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              start_new_session=True) as process:
            try:
                seen = wait_for(process.stdout, 'Количество [2]')
                os.write(master, b'1\n')
                seen = wait_for(process.stdout, 'root password (Enter', seen)
                os.write(master, b'changed entry pw\n')
                seen = wait_for(process.stdout, 'IP-адрес [198.51.100.2]', seen)
                os.write(master, b'\n')
                wait_for(process.stdout, 'root password (Enter', seen[seen.rindex(b'IP-'):])
                deadline = time.monotonic() + 10
                while termios.tcgetattr(slave)[3] & termios.ECHO:
                    assert time.monotonic() < deadline, 'пароль спрашивается при включённом ECHO'
                    time.sleep(0.01)
                os.kill(process.pid, number)
                output = process.communicate(timeout=10)[0].decode()
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
        assert process.returncode == -number, output
        assert termios.tcgetattr(slave)[3] & termios.ECHO
        assert 'Traceback' not in output
        assert config.read_bytes() == before
        assert [path.name for path in root.glob('.config-*')] == []
    finally:
        os.close(master)
        os.close(slave)


def test_cancel_at_save_keeps_filled_config(tmp_path):
    root, config = filled(tmp_path)
    before = config.read_bytes()
    answers = ['1', 'changed', '', '', '', 'other', '', '', 'n', 'n', 'n', 'n', 'n']
    code, output = rerun(root, lines(*answers))
    assert code == 0, output
    assert 'Отменено.' in output
    assert config.read_bytes() == before


def value(config, key):
    line = next(line for line in config.read_text().splitlines() if line.strip().startswith(key + ':'))
    return line.split(':', 1)[1].strip()


def test_new_passphrase_only_by_explicit_input_with_warning(tmp_path):
    root, config = filled(tmp_path)
    before = config.read_text()
    old = value(config, 'passphrase')
    # 23 вопроса до passphrase: Enter, затем новое значение.
    code, output = rerun(root, '\n' * 23 + 'brand new passphrase\n' + ENTER)
    assert code == 0, output
    assert config.read_text() == before.replace(f'passphrase: {old}', "passphrase: 'brand new passphrase'")
    assert 'Enter — оставить прежний' in output
    assert 'Passphrase изменён: архивы, сделанные раньше, открывает только прежний.' in output
    assert 'brand new passphrase' not in output and old.strip("'") not in output
    assert "BACKUP_PASSPHRASE='brand new passphrase'" in (root / 'infra/envs/ru.env').read_text()


def test_explicit_answers_change_only_their_fields(tmp_path):
    root, config = filled(tmp_path)
    before = config.read_text()
    answers = ['', 'new entry pw', '', '', '', '', '', '',          # entry и оба экзита
               'chief', 'NewAdminPw-2', 'NewAdminPw-2', '-',       # логин, пароль, ключ убран
               'n',                                                # домен убран
               '', '', '777', '-',                                 # бот: chat id, ник убран
               '', 'n', '', '',                                    # бэкап: расписание off
               'n', 'y']
    code, output = rerun(root, lines(*answers))
    assert code == 0, output
    expected = (before.replace("password: 'entry pw'", "password: 'new entry pw'")
                .replace("user: 'boss'", "user: 'chief'")
                .replace("password: 'OwnAdminPw-1'", "password: 'NewAdminPw-2'")
                .replace('ssh_pubkey: "ssh-ed25519 AAAAC3Nza me@host"', "ssh_pubkey: ''")
                .replace("domain: 'vpn.example.com'", "domain: ''")
                .replace("chat_id: '4242'", "chat_id: '777'")
                .replace("admin_username: 'boss'", "admin_username: ''")
                .replace("schedule: 'daily'", "schedule: 'off'")
                .replace("route: 'zapret'", "route: 'off'"))
    assert config.read_text() == expected
    assert 'config.yaml без изменений' not in output


def test_declined_bot_and_backup_are_switched_off_but_secrets_stay(tmp_path):
    root, config = filled(tmp_path)
    before = config.read_text()
    code, output = rerun(root, '\n' * 13 + 'n\nn\n' + ENTER)
    assert code == 0, output
    after = config.read_text()
    assert "bot_token: ''" in after and "chat_id: ''" in after and "admin_username: ''" in after
    assert 'enabled: false' in after
    assert value(config, 'passphrase') == before.split('passphrase: ')[1].split('\n')[0]


def test_replaced_exit_needs_its_own_password(tmp_path):
    root, config = filled(tmp_path)
    before = config.read_text()
    # Адрес первого экзита сменён: пароль прежнего сервера к нему не подходит.
    code, output = rerun(root, lines('', '', '198.51.100.9', '', 'pw of new server', 'nl') + ENTER)
    assert code == 0, output
    assert output.count('Пароль не может быть пустым') == 1
    assert config.read_text() == (
        before.replace("  - tag: de\n    host: '198.51.100.2'\n    password: 'exit pw 1'",
                       "  - tag: nl\n    host: '198.51.100.9'\n    password: 'pw of new server'"))


def test_mismatched_or_invalid_answers_keep_stored_values(tmp_path):
    root, config = filled(tmp_path)
    before = config.read_bytes()
    answers = ['', '', '', '', '', '', '', '', '', 'NewAdminPw-2', 'typo', 'not-a-key']
    code, output = rerun(root, lines(*answers) + ENTER)
    assert code == 0, output
    assert 'Пароли не совпадают — оставлен прежний пароль' in output
    assert 'Не похоже на SSH-ключ' in output and 'оставлен прежний ключ' in output
    assert config.read_bytes() == before


def test_broken_config_stops_before_questions(tmp_path):
    text = 'panel:\n  image: "CANARY unclosed\n'
    root, config = filled(tmp_path, text)
    code, output = rerun(root)
    assert code == 1, output
    assert 'ERROR: config.yaml: panel.image' in output and 'CANARY' not in output
    assert 'Количество' not in output
    assert config.read_text() == text
