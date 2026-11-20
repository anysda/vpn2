"""Мастер установки: токен и сессии, черновик без секретов, ключ бэкапов, запуск установки, конец мастера.

Настоящий HTTP-сервер мастера на свободном порту; менеджер установки - подставной (vpn2.manager.call).
"""
import http.client
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from vpn2 import config, creds, manager, wizard

KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl admin@laptop'
FAKE_AGE = '''#!PYTHON
import hashlib, sys
if sys.argv[1:] == ['-y']:
    print('age1' + hashlib.sha256(sys.stdin.read().encode()).hexdigest()[:40])
else:
    import secrets
    print('# public key: x')
    print('AGE-SECRET-KEY-1' + secrets.token_hex(20).upper())
'''


class Manager:
    """Подставной менеджер: задания в памяти, вызовы записаны."""

    def __init__(self):
        self.calls, self.jobs, self.problems, self.down = [], {}, [], False

    def __call__(self, method, params, **_options):
        self.calls.append((method, params))
        if self.down:
            raise ConnectionRefusedError
        if method == 'exit.check':
            return {'api': 1, 'ok': True, 'result': {'problems': self.problems}}
        if method == 'install.run':
            job = {'id': len(self.jobs) + 1, 'kind': 'install', 'state': 'running', 'stages': [], 'skipped': [],
                   'log': '', 'error': '', 'actions': []}
            self.jobs[job['id']] = job
            return {'api': 1, 'ok': True, 'result': job}
        if method == 'job.read':
            return {'api': 1, 'ok': True, 'result': self.jobs[params['job']]}
        if method == 'job.skip':
            return {'api': 1, 'ok': True, 'result': {'refused': 'без последнего экзита установка не продолжается'}}
        if method == 'status.read':
            return {'api': 1, 'ok': True, 'result': {'deploy': 'idle', 'job': None, 'nodes': [
                {'tag': 'ru', 'role': 'entry', 'host': '192.0.2.1', 'monitoring': 'up'},
                {'tag': 'nl', 'role': 'exit', 'host': '198.51.100.7', 'monitoring': 'up'}]}}
        raise AssertionError(method)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for name, path in (('STATE', 'state'), ('RUN', 'run'), ('STATIC', 'static'), ('CADDY', 'caddy')):
        monkeypatch.setattr(wizard, name, tmp_path / path)
    monkeypatch.setattr(wizard, 'HANDOVER', tmp_path / 'state/handover')
    (tmp_path / 'static').mkdir()
    (tmp_path / 'static/index.html').write_text('<!doctype html>wizard')
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'bin/age-keygen').write_text(FAKE_AGE.replace('PYTHON', sys.executable))
    (tmp_path / 'bin/age-keygen').chmod(0o755)
    monkeypatch.setenv('PATH', f'{tmp_path}/bin:{os.environ["PATH"]}')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    (tmp_path / 'home/.ssh').mkdir(parents=True)
    (tmp_path / 'home/.ssh/id_ed25519.pub').write_text('ssh-ed25519 AAAAorchestrator anysda-orchestrator\n')
    (tmp_path / 'state').mkdir()
    (tmp_path / 'state/entry.json').write_text('{"host": "192.0.2.1"}')
    fake = Manager()
    monkeypatch.setattr(manager, 'call', fake)
    wiz = wizard.Wizard()
    wiz.config = tmp_path / 'config.yaml'
    wizard.Handler.wizard = wiz
    server = ThreadingHTTPServer(('127.0.0.1', 0), wizard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Client(server.server_port), fake, wiz, tmp_path
    server.shutdown()
    server.server_close()


class Client:
    def __init__(self, port):
        self.port, self.cookie = port, ''

    def __call__(self, method, path, body=None, address='203.0.113.5'):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'X-Forwarded-For': address, **({'Cookie': f'{wizard.COOKIE}={self.cookie}'} if self.cookie else {})}
        connection.request(method, path, None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        data = response.read()
        if cookie := response.getheader('Set-Cookie'):
            self.cookie = cookie.split(';')[0].split('=', 1)[1]
            self.set_cookie = cookie
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, data.decode()

    def login(self, token):
        return self('POST', '/api/session', {'token': token})


def filled(client):
    """Черновик и секреты до кнопки «Установить»: экзит проверен, ключ бэкапов сохранён."""
    assert client('PUT', '/api/draft', {'admin_ssh_key': KEY, 'exits': [
        {'tag': 'nl', 'host': 'root@198.51.100.7', 'skipped': False},
        {'tag': 'de', 'host': '198.51.100.8', 'skipped': True}]})[0] == 200
    assert client('POST', '/api/exit/check', {'tag': 'nl'}) == (200, {'problems': []})
    assert client('PUT', '/api/secret/admin_password', {'value': 'correct horse 42'})[0] == 200
    issued = client('POST', '/api/backup-key')[1]
    assert client('POST', '/api/backup-key/seen', {'recipient': issued['recipient']}) == (200, {})
    return issued


def test_nothing_without_a_session_but_the_static_page(setup):
    client, _fake, _wiz, _tmp = setup
    wizard.new_link()
    assert client('GET', '/') == (200, '<!doctype html>wizard')
    assert client('GET', '/assets/../../state/entry.json') == (200, '<!doctype html>wizard')
    for method, path in (('GET', '/api/state'), ('GET', '/api/status'), ('PUT', '/api/draft'), ('POST', '/api/install'),
                         ('GET', '/api/nothing')):
        assert client(method, path, {}) == (401, {'refused': wizard.EXPIRED}), path


def test_token_cookie_expiry_replacement_and_attempt_limit(setup, monkeypatch):
    client, _fake, _wiz, _tmp = setup
    token = wizard.new_link()
    assert client.login('wrong')[0] == 401
    assert client.login(token) == (200, {})
    assert client.set_cookie.endswith('; Path=/; HttpOnly; Secure; SameSite=Strict')
    state = client('GET', '/api/state')[1]
    assert state['phase'] == 'filling' and state['link_expires_at'] > time.time() + 3500
    assert state['orchestrator_key'] == 'ssh-ed25519 AAAAorchestrator anysda-orchestrator'
    # Вторая вкладка с той же ссылкой - своя сессия, тот же черновик.
    other = Client(client.port)
    assert other.login(token)[0] == 200 and other('GET', '/api/state')[0] == 200
    # Новый ./deploy.sh: прежние токен и сессии мертвы.
    fresh = wizard.new_link()
    assert client('GET', '/api/state')[0] == 401 and client.login(token)[0] == 401
    assert client.login(fresh)[0] == 200
    # Час прошёл: ни токен, ни сессия до начала установки не действуют.
    later = time.time() + wizard.LINK_LIFE + 1
    with monkeypatch.context() as patched:
        patched.setattr(wizard.time, 'time', lambda: later)
        assert client('GET', '/api/state')[0] == 401 and Client(client.port).login(fresh)[0] == 401
    # Подбор: после 10 неверных с адреса и верный токен не принимается, с другого адреса - да.
    fresh = wizard.new_link()
    for _ in range(wizard.ATTEMPTS):
        assert client('POST', '/api/session', {'token': 'x'}, address='198.18.0.9')[0] == 401
    assert client('POST', '/api/session', {'token': fresh}, address='198.18.0.9')[0] == 429
    assert client('POST', '/api/session', {'token': fresh})[0] == 200


def test_token_is_not_kept_or_logged(setup, capfd):
    client, _fake, _wiz, tmp = setup
    token = wizard.new_link()
    client.login(token)
    client('GET', '/api/state')
    assert token not in (tmp / 'run/token.json').read_text()
    assert oct((tmp / 'run/token.json').stat().st_mode & 0o777) == '0o600'
    out, err = capfd.readouterr()
    assert token not in out + err and client.cookie not in out + err and '/api/' not in err


def test_draft_is_validated_merged_and_keeps_no_secret(setup):
    client, fake, _wiz, tmp = setup
    client.login(wizard.new_link())
    assert client('PUT', '/api/draft', {'exits': [{'tag': 'RU', 'host': '1.2.3.4', 'skipped': False}]})[0] == 400
    assert client('PUT', '/api/draft', {'exits': [{'tag': 'nl', 'host': 'admin@1.2.3.4', 'skipped': False}]})[0] == 400
    code, body = client('PUT', '/api/draft', {'panel': {'domain': 'https://vpn.example.com', 'admin_user': 'anysda'}})
    assert code == 400 and 'Домен' in body['refused']
    assert client('PUT', '/api/draft', {'admin_ssh_key': 'not a key'})[0] == 400
    assert client('PUT', '/api/draft', {'panel': {'domain': 'VPN.Example.com', 'admin_user': 'boss'}, 'step': 3})[0] == 200
    draft = client('PUT', '/api/draft', {'exits': [{'tag': 'nl', 'host': 'root@198.51.100.7', 'skipped': False}]})[1]['draft']
    assert draft['panel'] == {'domain': 'vpn.example.com', 'admin_user': 'boss'} and draft['step'] == 3
    assert draft['exits'] == [{'tag': 'nl', 'host': '198.51.100.7', 'skipped': False, 'problems': None}]
    fake.problems = ['198.51.100.7 не пускает по ключу оркестратора']
    assert client('POST', '/api/exit/check', {'tag': 'nl'}) == (200, {'problems': fake.problems})
    assert fake.calls[-1] == ('exit.check', {'tag': 'nl', 'host': '198.51.100.7'})
    # Тот же адрес - итог проверки остаётся, другой - сбрасывается.
    same = client('PUT', '/api/draft', {'exits': [{'tag': 'nl', 'host': '198.51.100.7', 'skipped': True}]})[1]['draft']
    assert same['exits'][0]['problems'] == fake.problems
    moved = client('PUT', '/api/draft', {'exits': [{'tag': 'nl', 'host': '198.51.100.9', 'skipped': False}]})[1]['draft']
    assert moved['exits'][0]['problems'] is None
    for name, value in (('admin_password', 'correct horse 42'), ('telegram_bot_token', '123:ABC'), ('s3_secret_key', 'S3SECRET')):
        assert client('PUT', f'/api/secret/{name}', {'value': value})[0] == 200
    assert client('PUT', '/api/secret/admin_password', {'value': 'short'})[0] == 400
    state = client('GET', '/api/state')[1]
    assert state['secrets'] == {'admin_password': True, 'telegram_bot_token': True, 's3_access_key': False, 's3_secret_key': True}
    assert creds.read('telegram_bot_token') == '123:ABC' and creds.read('s3_secret_key') == 'S3SECRET'
    on_disk = ''.join(path.read_text() for path in (tmp / 'state').rglob('*.json'))
    for secret in ('correct horse 42', '123:ABC', 'S3SECRET'):
        assert secret not in on_disk and secret not in json.dumps(state)
    assert oct((tmp / 'state/handover/admin_password').stat().st_mode & 0o777) == '0o600'
    # Перезапуск мастера: черновик тот же.
    assert wizard.Wizard().draft()['panel']['admin_user'] == 'boss'


def test_backup_key_is_shown_until_confirmed_and_never_kept(setup):
    client, _fake, _wiz, tmp = setup
    client.login(wizard.new_link())
    first = client('POST', '/api/backup-key')[1]
    second = client('POST', '/api/backup-key')[1]
    assert first['private_key'].startswith('AGE-SECRET-KEY-1') and second['recipient'] != first['recipient']
    assert client('GET', '/api/state')[1]['backup_key'] == 'shown'
    # Первая вкладка подтверждает ключ, который вторая уже заменила.
    assert client('POST', '/api/backup-key/seen', {'recipient': first['recipient']})[0] == 409
    assert client('POST', '/api/backup-key/seen', {'recipient': second['recipient']}) == (200, {})
    assert client('POST', '/api/backup-key')[0] == 409
    assert client('GET', '/api/state')[1]['backup_key'] == 'seen'
    files = ''.join(path.read_text() for path in tmp.rglob('*') if path.is_file() and 'bin' not in path.parts)
    assert first['private_key'] not in files and second['private_key'] not in files
    assert (tmp / 'state/handover/backup.pub').read_text() == second['recipient'] + '\n'
    assert (tmp / 'state/backup/etc/anysda/backup.pub').is_file(), 'пара мастера - не в /etc/anysda до установки'


def test_install_refuses_until_complete_then_writes_config_without_secrets(setup):
    client, fake, _wiz, tmp = setup
    client.login(wizard.new_link())
    code, body = client('POST', '/api/install')
    assert code == 409 and 'ключ ssh администратора' in body['refused'] and 'хотя бы один экзит' in body['refused']
    filled(client)
    client('PUT', '/api/draft', {'telegram': {'enabled': True, 'chat_id': '-1001234'}, 'youtube': True,
                                 'panel': {'domain': 'vpn.example.com', 'admin_user': 'anysda'}})
    code, body = client('POST', '/api/install')
    assert code == 409 and body['refused'] == 'Для установки не хватает: токен бота и чат Telegram (экран 3) - или отключите бота'
    client('PUT', '/api/secret/telegram_bot_token', {'value': '123:ABC'})
    code, body = client('POST', '/api/install')
    assert code == 200 and body['job']['id'] == 1 and ('install.run', {}) in fake.calls
    text = (tmp / 'config.yaml').read_text()
    cfg = config.loads(text)
    assert cfg.entry.host == '192.0.2.1' and [(node.tag, node.host) for node in cfg.exits] == [('nl', '198.51.100.7')]
    assert cfg.raw['panel'] == {'domain': 'vpn.example.com'} and cfg.raw['telegram'] == {'chat_id': '-1001234'}
    assert cfg.raw['youtube'] == {'route': 'zapret'} and cfg.raw['admin']['ssh_pubkey'] == KEY
    assert 'correct horse' not in text and '123:ABC' not in text and 'secrets' not in cfg.raw
    assert oct((tmp / 'config.yaml').stat().st_mode & 0o777) == '0o600'
    # Установка идёт: обе вкладки только смотрят, сессия живёт дольше часа ссылки.
    state = client('GET', '/api/state')[1]
    assert state['phase'] == 'installing' and state['link_expires_at'] is None and state['job']['id'] == 1
    assert client('PUT', '/api/draft', {'youtube': False})[0] == 409
    assert client('PUT', '/api/secret/admin_password', {'value': 'another password'})[0] == 409
    assert client('POST', '/api/install')[0] == 409
    sessions = json.loads((tmp / 'run/token.json').read_text())['sessions']
    assert all(item['expires'] is None for item in sessions.values())


def test_job_actions_failure_text_and_manager_restart(setup):
    client, fake, _wiz, _tmp = setup
    client.login(wizard.new_link())
    filled(client)
    client('POST', '/api/install')
    fake.jobs[1].update(state='failed', actions=['retry', 'skip', 'forget'])
    assert client('POST', '/api/job/skip', {'tag': 'nl'}) == (409, {'refused': 'без последнего экзита установка не продолжается'})
    assert fake.calls[-1] == ('job.skip', {'job': 1, 'tag': 'nl'})
    # Менеджер перезапускается (стадия 29-control): страница получает 503 и повторяет тихо.
    fake.down = True
    assert client('GET', '/api/state')[0] == 503
    fake.down = False
    assert client('GET', '/api/state')[1]['phase'] == 'installing'


def test_done_status_then_the_wizard_leaves(setup, monkeypatch):
    client, fake, wiz, tmp = setup
    token = wizard.new_link()
    client.login(token)
    filled(client)
    calls = []
    monkeypatch.setattr(wizard.subprocess, 'run', lambda argv, **_: calls.append(argv))
    from vpn2 import tls
    monkeypatch.setattr(tls, 'serve_routes', lambda *args: calls.append(['routes', *args]))
    client('POST', '/api/install')
    fake.jobs[1].update(state='done', skipped=['de'])
    assert client('GET', '/api/state')[1]['phase'] == 'done'
    status = client('GET', '/api/status')[1]
    assert status['panel_url'] == 'https://192.0.2.1/'
    states = {item['name']: item['state'] for item in status['systems']}
    assert states['Экзит nl'] == 'ok' and states['Экзит de'] == 'warning' and states['Telegram-бот'] == 'warning'
    now = time.time()
    wiz.tick(now)
    assert Client(client.port).login(token)[0] == 401, 'ссылка жива после конца установки'
    wiz.seen = now
    wiz.tick(now + 30)
    assert (tmp / 'state').exists(), 'страница открыта, итог ещё читается'
    wiz.tick(now + wizard.DONE_LIFE + 1)
    assert not (tmp / 'state').exists() and not (tmp / 'run/token.json').exists()
    assert ['routes', 'wizard', ''] in calls and ['systemctl', 'disable', 'vpn2-wizard'] in calls
    assert client('GET', '/api/status')[0] == 401


@pytest.mark.parametrize(('argv', 'env', 'files', 'expected'), [
    ([], {}, (), True),
    (['--development'], {}, (), False),
    ([], {'VPN2_JOB': '/var/lib/vpn2-manager/jobs/1'}, (), False),
    ([], {}, ('config.yaml',), False),
    ([], {}, ('state/draft.json', 'config.yaml'), True),
    (['30-frontend', 'ru'], {}, ('state/draft.json',), False),
])
def test_wanted_only_on_a_clean_system_or_a_started_wizard(argv, env, files, expected, tmp_path, monkeypatch):
    from vpn2 import deploy
    monkeypatch.setattr(wizard, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(deploy, 'CONFIG', tmp_path / 'config.yaml')
    monkeypatch.setattr(deploy, 'installed', lambda: False)
    for name in files:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text('{}')
    assert wizard.wanted(argv, env) is expected


def test_installed_system_and_finished_install_never_start_the_wizard(tmp_path, monkeypatch):
    from vpn2 import deploy
    monkeypatch.setattr(wizard, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(deploy, 'CONFIG', tmp_path / 'config.yaml')
    monkeypatch.setattr(deploy, 'installed', lambda: True)
    assert wizard.wanted([], {}) is False
    (tmp_path / 'state').mkdir()
    (tmp_path / 'state/draft.json').write_text('{}')
    (tmp_path / 'state/state.json').write_text('{"job": 3}')
    monkeypatch.setattr(manager.Manager, 'load', lambda _self, number: {'id': number, 'state': 'done'})
    assert wizard.wanted([], {}) is False
    monkeypatch.setattr(manager.Manager, 'load', lambda _self, number: {'id': number, 'state': 'failed'})
    assert wizard.wanted([], {}) is True


def test_admin_password_reaches_the_deploy_and_leaves_after_the_panel_stage(tmp_path, monkeypatch):
    from vpn2 import deploy
    monkeypatch.setattr(wizard, 'HANDOVER', tmp_path / 'handover')
    (tmp_path / 'handover').mkdir()
    (tmp_path / 'handover/admin_password').write_text('correct horse 42')
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(f'entry:\n  host: 192.0.2.1\nexits:\n  - tag: nl\n    host: root@198.51.100.7\nadmin:\n  ssh_pubkey: "{KEY}"\n')
    monkeypatch.setattr(deploy, 'CONFIG', cfg)
    assert deploy.take_secrets() == 'correct horse 42'
    deploy.finish_secrets(panel=False)
    assert (tmp_path / 'handover/admin_password').exists(), 'стадии панели не было: пароль ещё нужен повтору'
    deploy.finish_secrets(panel=True)
    assert not (tmp_path / 'handover/admin_password').exists()
