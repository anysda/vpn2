"""Менеджер установки: отказы до изменений, задание на диске, замок с deploy.sh, ход для второго читателя."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from vpn2 import config, fsutil, manager
from vpn2.deploy import run as deploy_run

CONFIG = ('entry:\n  host: 192.0.2.1\nexits:\n  - tag: se\n    host: root@192.0.2.2\n'
          '  - tag: at\n    host: 192.0.2.3\nadmin:\n  user: a\n')


class Fake(manager.Manager):
    """Процесс задания - запись запуска; живость задаёт тест."""

    def __init__(self, root):
        (root / 'config.yaml').write_text(CONFIG)
        super().__init__(root / 'state', root / 'config.yaml', root / 'deploy.lock', root)
        self.running, self.spawned, self.logins, self.recovers = False, [], [], []
        self.journal = root / 'operation.json'

    def alive(self):
        return self.running

    def spawn(self, _folder, argv, recover):
        self.spawned.append(argv)
        self.recovers.append(recover)
        self.running = True

    def login_problems(self, address):
        self.logins.append(address)
        return []

    def end(self, code, stages=()):
        """Процесс задания кончился с кодом code (None - пропал без итога)."""
        folder = self.folder(self.latest())
        fsutil.write_json(folder / 'progress.json', {'stages': list(stages), 'exit': code})
        self.running = False


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, 'local_addresses', lambda: {'127.0.0.1', '10.0.0.5'})
    return Fake(tmp_path)


def call(fake, method, **params):
    return manager.answer(fake, json.dumps({'api': 1, 'method': method, 'params': params}))['result']


def tags(fake):
    return [node.tag for node in config.load(fake.config).exits]


def test_requests_are_only_the_fixed_actions(fake):
    bad = [{'api': 1, 'method': 'exit.add', 'params': {'tag': 'nl'}},
           {'api': 1, 'method': 'exit.add', 'params': {'tag': 'nl', 'host': '1.2.3.4', 'path': '/etc'}},
           {'api': 1, 'method': 'exit.add', 'params': {'tag': 'n l', 'host': '1.2.3.4'}},
           {'api': 1, 'method': 'exit.remove', 'params': {'tag': ['se']}},
           {'api': 1, 'method': 'job.read', 'params': {'job': '1'}},
           {'api': 1, 'method': 'shell', 'params': {}},
           {'api': 2, 'method': 'status.read', 'params': {}}]
    for request in bad:
        assert manager.answer(fake, json.dumps(request)) == {'api': 1, 'ok': False, 'error': 'validation'}
    assert manager.answer(fake, b'{') == {'api': 1, 'ok': False, 'error': 'validation'}
    assert fake.spawned == []


@pytest.mark.parametrize('tag, host, text', [
    ('se', '192.0.2.9', 'тег se уже занят'),
    ('nl', 'root@192.0.2.3', 'адрес 192.0.2.3 уже у экзита at'),
    ('nl', '192.0.2.1', '192.0.2.1 - это сама entry'),
    ('nl', '10.0.0.5', '10.0.0.5 - это сама entry'),
    ('NL', '192.0.2.9', 'тег «NL»: строчные латинские буквы'),
    ('ru', '192.0.2.9', 'тег «ru»'),
    ('nl', 'admin@192.0.2.9', 'вход на экзит только root'),
    ('nl', 'bad_host!', 'ожидается IP-адрес или DNS-имя'),
])
def test_add_refused_before_any_change(fake, tag, host, text):
    reply = call(fake, 'exit.add', tag=tag, host=host)
    assert text in reply['refused']
    assert (fake.config.read_text(), fake.spawned, fake.logins, fake.latest()) == (CONFIG, [], [], 0)


@pytest.mark.parametrize('rc, out, err, text', [
    (255, '', 'root@192.0.2.9: Permission denied (publickey).', 'не пускает по ключу оркестратора'),
    (255, '', 'ssh: connect to host 192.0.2.9 port 22: No route to host', 'не отвечает по ssh: ssh: connect'),
    (255, '', '@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@', 'сменился ключ хоста'),
    (0, '0\ndebian 13\n', '', 'на 192.0.2.9 debian 13: нужна Ubuntu 26.04'),
    (0, '0\nubuntu 24.04\n', '', 'нужна Ubuntu 26.04'),
    (0, '1000\nubuntu 26.04\n', '', 'вход не под root'),
    (0, '0\nubuntu 26.04\n', '', None),
])
def test_exit_check_login_and_os(tmp_path, monkeypatch, rc, out, err, text):
    checker = manager.Manager(tmp_path, tmp_path / 'config.yaml', tmp_path / 'lock', tmp_path)
    (tmp_path / 'config.yaml').write_text(CONFIG)
    monkeypatch.setattr(manager, 'local_addresses', set)
    seen = []

    def run(command, **_options):
        seen.append(command)
        return subprocess.CompletedProcess(command, rc, out, err)

    monkeypatch.setattr(subprocess, 'run', run)
    problems = checker.problems('nl', 'root@192.0.2.9')
    assert problems == [] if text is None else text in problems[0]
    assert seen[0][0] == 'ssh' and 'root@192.0.2.9' in seen[0] and 'BatchMode=yes' in seen[0]


def test_add_job_failure_restores_config_then_retry_and_forget(fake):
    job = call(fake, 'exit.add', tag='nl', host='root@192.0.2.9')
    assert (job['id'], job['kind'], job['state'], job['host']) == (1, 'add', 'running', '192.0.2.9')
    assert fake.spawned == [['exit-add', 'nl']] and tags(fake) == ['se', 'at', 'nl']
    assert 'идёт задание 1' in call(fake, 'exit.remove', tag='se')['refused']
    # Упала стадия после того, как экзит уже лёг: в config.yaml его нет, задание ждёт повтора или «забыть».
    fake.end(1, [{'name': '10-foreign', 'state': 'done', 'nodes': ['nl'], 'failed': []},
                 {'name': '20-ru-router', 'state': 'failed', 'nodes': ['ru'], 'failed': ['ru'], 'offset': 0}])
    (fake.folder(1) / 'log').write_text('[deploy] FATAL: стадия 20-ru-router: одна или несколько нод завершились с ошибкой\n')
    # Копия ключа оркестратора, что деплой дописал за задание, переживает возврат состава.
    with open(fake.config, 'a') as text:
        text.write('orchestrator_key: QUJD\n')
    job = call(fake, 'job.read', job=0)
    assert (job['state'], job['actions']) == ('failed', ['retry', 'forget'])
    assert fake.config.read_text() == CONFIG + 'orchestrator_key: QUJD\n'
    assert job['error'] == 'стадия 20-ru-router: одна или несколько нод завершились с ошибкой'
    assert job['stages'][1] == {'name': '20-ru-router', 'state': 'failed', 'nodes': ['ru'], 'failed': ['ru']}
    assert 'не закрыто' in call(fake, 'exit.add', tag='de', host='192.0.2.8')['refused']
    assert call(fake, 'job.retry', job=1)['state'] == 'running'
    assert fake.spawned[-1] == ['exit-add', 'nl'] and tags(fake) == ['se', 'at', 'nl']
    fake.end(1, [{'name': '10-foreign', 'state': 'done', 'nodes': ['nl'], 'failed': []}])
    # «Забыть» убирает то, что легло: задание удаления без экзита в config.yaml.
    cleanup = call(fake, 'job.forget', job=1)
    assert (cleanup['id'], cleanup['kind'], cleanup['tag'], cleanup['host']) == (2, 'remove', 'nl', '192.0.2.9')
    assert fake.spawned[-1] == ['exit-remove', 'nl', '192.0.2.9'] and tags(fake) == ['se', 'at']
    assert fake.load(1)['state'] == 'forgotten'
    fake.end(0)
    assert call(fake, 'job.read', job=2)['state'] == 'done' and tags(fake) == ['se', 'at']
    assert config.load(fake.config).raw['orchestrator_key'] == 'QUJD'


def test_forget_of_add_that_laid_nothing_closes_it(fake):
    call(fake, 'exit.add', tag='nl', host='192.0.2.9')
    fake.end(1, [{'name': 'preflight', 'state': 'failed', 'nodes': ['ru', 'nl'], 'failed': ['nl']},
                 {'name': '00-bootstrap', 'state': 'pending', 'nodes': [], 'failed': []}])
    assert call(fake, 'job.forget', job=1)['state'] == 'forgotten'
    assert (fake.latest(), tags(fake)) == (1, ['se', 'at'])


def test_release_check_refusal_names_the_node(fake):
    call(fake, 'exit.add', tag='nl', host='192.0.2.9')
    (fake.folder(1) / 'log').write_text(
        'fatal: [nl]: FAILED! => {"changed": false, "msg": "non-zero return code", "stderr": "deploy stopped: no space"}\n')
    fake.end(1, [{'name': 'preflight', 'state': 'failed', 'nodes': ['ru', 'nl'], 'failed': ['nl']}])
    job = call(fake, 'job.read', job=1)
    assert (job['error'], job['stages'][0]['failed']) == ('nl: deploy stopped: no space', ['nl'])
    assert call(fake, 'job.forget', job=1)['state'] == 'forgotten'


def test_remove_rules(fake):
    assert 'экзита nl нет' in call(fake, 'exit.remove', tag='nl')['refused']
    job = call(fake, 'exit.remove', tag='at')
    assert fake.spawned == [['exit-remove', 'at', '192.0.2.3']] and tags(fake) == ['se']
    fake.end(0)
    assert call(fake, 'job.read', job=job['id'])['state'] == 'done'
    assert 'последний экзит удалить нельзя' in call(fake, 'exit.remove', tag='se')['refused']
    assert tags(fake) == ['se'] and len(fake.spawned) == 1


def test_interrupted_job_after_restart_or_reboot(fake):
    call(fake, 'exit.remove', tag='at')
    (fake.folder(1) / 'progress.json').write_text(json.dumps(
        {'stages': [{'name': '20-ru-router', 'state': 'running', 'nodes': ['ru'], 'failed': []}]}))
    fake.running = False  # процесс пропал без итога: kill -9 или перезагрузка
    again = Fake.__new__(Fake)
    manager.Manager.__init__(again, fake.jobs.parent, fake.config, fake.lock, fake.repo)
    again.running, again.spawned, again.recovers = False, [], []
    job = call(again, 'job.read', job=1)
    assert (job['state'], job['actions'], tags(again)) == ('interrupted', ['retry', 'forget'], ['se', 'at'])
    assert job['stages'][0]['state'] == 'failed'
    assert call(again, 'job.retry', job=1)['state'] == 'running' and again.spawned == [['exit-remove', 'at', '192.0.2.3']]


def test_install_continues_without_a_failed_exit(fake):
    fake.journal.write_text('{"phase": "applying"}')
    assert 'сначала ./deploy.sh recover' in call(fake, 'install.run')['refused']
    fake.journal.write_text('{"phase": "complete"}')
    call(fake, 'install.run')
    assert fake.spawned == [[]] and fake.config.read_text() == CONFIG
    fake.end(1, [{'name': '10-foreign', 'state': 'failed', 'nodes': ['se', 'at'], 'failed': ['at']}])
    assert call(fake, 'job.read', job=1)['actions'] == ['retry', 'skip', 'forget']
    assert 'экзита nl нет' in call(fake, 'job.skip', job=1, tag='nl')['refused']
    assert call(fake, 'job.skip', job=1, tag='at')['skipped'] == ['at']
    # Упавшая установка оставила журнал открытым: продолжение - recover.
    assert tags(fake) == ['se'] and fake.spawned[-1] == ['recover'] and fake.recovers == [False, True]
    fake.end(1)
    assert call(fake, 'job.read', job=1)['state'] == 'failed' and tags(fake) == ['se', 'at']
    assert 'без последнего экзита' in call(fake, 'job.skip', job=1, tag='se')['refused']
    call(fake, 'job.retry', job=1)
    assert tags(fake) == ['se']
    fake.end(2)
    assert call(fake, 'job.read', job=1)['state'] == 'done' and tags(fake) == ['se']
    assert 'только у последнего упавшего' in call(fake, 'job.retry', job=1)['refused']


def hold(path):
    """Процесс держит замок deploy.sh (flock), как ./deploy.sh."""
    holder = subprocess.Popen([sys.executable, '-c', 'import fcntl, sys, time\nf = open(sys.argv[1], "w")\n'
                               'fcntl.flock(f, fcntl.LOCK_EX)\nprint(1, flush=True)\ntime.sleep(60)', str(path)],
                              stdout=subprocess.PIPE)
    holder.stdout.readline()
    return holder


def test_deploy_lock_both_ways_and_after_kill(fake):
    holder = hold(fake.lock)
    try:
        assert call(fake, 'install.run')['refused'] == 'идёт ./deploy.sh: дождись его конца'
        assert call(fake, 'status.read')['deploy'] == 'busy'
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait()
    # Замок после kill -9 держателя свободен: задание стартует.
    assert not fake.busy() and fake.spawned == []
    assert call(fake, 'exit.remove', tag='at')['state'] == 'running'


def test_progress_survives_second_reader_and_socket(fake, monkeypatch):
    monkeypatch.setattr(manager, 'callers', lambda: {os.getuid()})
    path = Path(f'/tmp/vpn2-manager-test-{os.getpid()}.sock')
    threading.Thread(target=manager.serve, args=(fake, path), daemon=True).start()
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.02)
    try:
        assert manager.call('exit.remove', {'tag': 'at'}, path)['result']['state'] == 'running'
        progress = manager.Progress(fake.folder(1))
        progress.plan(['preflight', '20-ru-router'])
        with progress.stage('preflight', ['ru', 'se']):
            pass
        (fake.folder(1) / 'log').write_text('старое\n')
        with pytest.raises(SystemExit), progress.stage('20-ru-router', ['ru']):
            with open(fake.folder(1) / 'log', 'a') as log:
                log.write('PLAY RECAP\nru  : ok=3 changed=1 unreachable=0 failed=1 skipped=0\n')
            raise SystemExit(1)
        reply = manager.call('job.read', {'job': 1}, path)['result']
        assert [item['state'] for item in reply['stages']] == ['done', 'failed']
        assert reply['stages'][1]['failed'] == ['ru'] and reply['log'].startswith('PLAY RECAP')
        assert manager.call('job.read', {'job': 1, 'x': 1}, path) == {'api': 1, 'ok': False, 'error': 'validation'}
        status = manager.call('status.read', {}, path)['result']
        assert [node['tag'] for node in status['nodes']] == ['ru', 'se'] and status['job']['id'] == 1
    finally:
        path.unlink(missing_ok=True)


def test_progress_parts_of_one_stage(tmp_path):
    progress = manager.Progress(tmp_path)
    progress.plan(['23-emergency'])
    with progress.stage('23-emergency', ['se']):
        with progress.stage('23-emergency', ['ru']):
            pass
        assert fsutil.read_json(tmp_path / 'progress.json')['stages'][0]['state'] == 'running'
    progress.finish(0, {'x': 1})
    data = fsutil.read_json(tmp_path / 'progress.json')
    assert data['stages'][0]['state'] == 'done' and data['stages'][0]['nodes'] == ['ru', 'se'] and data['exit'] == 0
    assert 'exit' not in manager.Progress(tmp_path).data


@pytest.mark.parametrize('out, rc, server', [
    ('', 255, 'forgotten'),
    ('vpn2-forget: start\nvpn2-forget: done\n', 0, 'cleaned'),
    ('vpn2-forget: start\nleft: служба sing-box.service не остановилась\nvpn2-forget: done\n', 0, 'partial'),
    ('vpn2-forget: start\n', 255, 'partial'),
])
def test_forget_exit_cleans_entry_whatever_the_server_says(tmp_path, monkeypatch, out, rc, server):
    infra, state = tmp_path / 'infra', tmp_path / 'state'
    for name in ('exit-certs/nl.pem', 'exit-certs/nl.reality', 'exit-hostkeys/nl.pub', 'emergency/nl.json', 'exit-certs/se.pem'):
        (infra / 'secrets' / name).parent.mkdir(parents=True, exist_ok=True)
        (infra / 'secrets' / name).write_text('x')
    (infra / 'lib').mkdir()
    (infra / 'lib/exit-forget.sh').write_text('echo\n')
    state.mkdir()
    (state / 'mgmt-ips.txt').write_text('se 2\nnl 3\n')
    monkeypatch.setattr(deploy_run, 'INFRA', infra)
    monkeypatch.setattr(deploy_run, 'STATE_DIR', state)
    calls = []

    def run(command, **options):
        calls.append((command, options.get('input', '')))
        return subprocess.CompletedProcess(command, rc if command[0] == 'ssh' else 0, out, '')

    monkeypatch.setattr(subprocess, 'run', run)
    deploy = type('D', (), {'deploy_warnings': []})()
    result = deploy_run.forget_exit(deploy, 'nl', '192.0.2.9')
    assert result['server'] == server and (result['left'] == []) == (server != 'partial')
    assert sorted(path.name for path in (infra / 'secrets').rglob('*') if path.is_file()) == ['se.pem']
    assert (state / 'mgmt-ips.txt').read_text() == 'se 2\n'
    assert calls[0][0][-2:] == ['root@192.0.2.9', 'bash -s'] and calls[0][1].startswith('ENTRY_KEYS=')
    assert calls[1][0][:3] == ['ssh-keygen', '-R', '192.0.2.9']


def test_socket_refuses_other_users(monkeypatch):
    monkeypatch.setattr(manager, 'callers', lambda: {12345})
    root = Path(f'/tmp/vpn2-manager-uid-{os.getpid()}')
    root.mkdir(exist_ok=True)
    (root / 'config.yaml').write_text(CONFIG)
    path = root / 's.sock'
    threading.Thread(target=manager.serve, args=(Fake(root), path), daemon=True).start()
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.02)
    try:
        assert manager.call('status.read', {}, path) == {'api': 1, 'ok': False, 'error': 'forbidden'}
        assert path.stat().st_mode & 0o777 == 0o660
    finally:
        path.unlink(missing_ok=True)


def test_control_relays_to_manager(monkeypatch):
    from vpn2.control import backend
    sent = []
    monkeypatch.setattr(manager, 'call', lambda method, params, **_options: sent.append((method, params)) or
                        {'api': 1, 'ok': True, 'result': {'refused': 'x'}})
    assert backend.Backend.execute(object.__new__(backend.Backend), 'manager.exit.remove', {'tag': 'nl'}) == {'refused': 'x'}
    assert sent == [('exit.remove', {'tag': 'nl'})]
    monkeypatch.setattr(manager, 'call', lambda *_args, **_options: {'api': 1, 'ok': False, 'error': 'forbidden'})
    with pytest.raises(backend.Fault, match='unavailable'):
        backend.Backend.execute(object.__new__(backend.Backend), 'manager.status.read', {})

