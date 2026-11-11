"""Шаги стадий 25 и 35 и запуск службы: то, что прежде держали bash-стадии."""

import json
import subprocess

import pytest

from vpn2.containers import compose, engine, monitoring, telegram


class Run:
    """docker в памяти: inspect по имени, compose up создаёт контейнер проекта."""

    def __init__(self, containers=None):
        self.containers, self.calls, self.serial = dict(containers or {}), [], 0

    def __call__(self, argv, **_kwargs):
        args = argv[1:]
        self.calls.append(args)
        code, out = 0, ''
        if args[0] == 'inspect':
            found = self.containers.get(args[-1])
            code, out = (0, json.dumps([found])) if found else (1, '')
        elif args[:2] == ['rm', '-f']:
            self.containers.pop(args[2], None)
        elif args[0] == 'compose' and args[-1] not in self.containers:
            self.serial += 1
            self.containers[args[-1]] = {'Id': f'new{self.serial}', 'Config': {
                'Labels': {engine.PROJECT_LABEL: compose.PROJECT}}}
        return subprocess.CompletedProcess(argv, code, out, '')


def test_up_replaces_container_started_by_docker_run(tmp_path):
    """Обновление с выпуска на docker run: старый контейнер снят, новый создан compose."""
    run = Run({compose.VM: {'Id': 'old', 'Config': {'Labels': {}}}})
    docker = engine.Docker(run, tmp_path / 'compose/compose.yaml')
    assert docker.up(compose.VM) is True
    assert ['rm', '-f', compose.VM] in run.calls
    assert run.containers[compose.VM]['Id'] == 'new1'
    # Повтор: контейнер уже принадлежит проекту, его не снимают и не пересоздают.
    run.calls.clear()
    assert docker.up(compose.VM) is False
    assert not [call for call in run.calls if call[0] == 'rm']


def test_publish_writes_private_file_once(tmp_path):
    docker = engine.Docker(Run(), tmp_path / 'compose/compose.yaml')
    service = {'container_name': compose.BOT, 'image': 'sha256:1', 'environment': {'A': 'p$w'}}
    assert docker.publish(compose.BOT, service) is True
    path = tmp_path / 'compose/compose.yaml'
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert docker.publish(compose.BOT, service) is False
    assert docker.services() == {compose.BOT: service}


def test_every_service_restarts_and_bounds_its_log():
    text = 'ENTRY_DOCKER_ARGS=(--memory 64m --memory-swap 64m)\nENTRY_RUNTIME_ARGS=()\n'
    for service in (compose.vm_service('vm:1', text, 'scrape'),
                    compose.bot_service({'AGH_PORT': '3000'}, 'sha256:b', text, secret='s', user='1:1',
                                        panel_gid=2)):
        assert service['restart'] == 'always' and service['network_mode'] == 'host'
        assert service['logging'] == {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}
        assert service['mem_limit'] == service['memswap_limit'] == '64m'


def target(host, health='up'):
    return {'labels': {'host': host}, 'health': health, 'scrapeUrl': 'http://127.0.0.1:9100/metrics'}


@pytest.mark.parametrize('reply, ok', [
    ({'status': 'success', 'data': {'activeTargets': [target('ru'), target('se')]}}, True),
    ({'status': 'success', 'data': {'activeTargets': [target('ru'), target('se', 'down')]}}, False),
    ({'status': 'success', 'data': {'activeTargets': [target('ru')]}}, False),
    ({'status': 'error', 'data': {'activeTargets': [target('ru'), target('se')]}}, False),
    ({}, False),
])
def test_monitoring_is_done_only_with_every_target_up(tmp_path, monkeypatch, capsys, reply, ok):
    stamps = []
    monkeypatch.setattr(monitoring.os, 'makedirs', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(engine, 'drop_timezone_dir', lambda: None)
    monkeypatch.setattr(engine, 'resource_text', lambda *_args: 'ENTRY_DOCKER_ARGS=()\nENTRY_RUNTIME_ARGS=()\n')
    monkeypatch.setattr(engine, 'stamp', stamps.append)
    docker = engine.Docker(Run(), tmp_path / 'compose.yaml')
    environ = {'EXIT_TAGS': 'se', 'MGMT_IP_SE': '10.99.0.2', 'VM_VERSION': 'v1'}
    arguments = {'docker': docker, 'say': print, 'probe': lambda: reply, 'sleep': lambda _pause: None,
                 'scrape_path': tmp_path / 'scrape.yml'}
    if ok:
        assert monitoring.run(environ, **arguments) is True
        assert stamps == [monitoring.STAGE] and 'se (' in capsys.readouterr().out
        assert monitoring.run(environ, **arguments) is False
    else:
        with pytest.raises(engine.EngineError, match='ожидается 2'):
            monitoring.run(environ, **arguments)
        assert stamps == []


def test_monitoring_needs_exits(tmp_path):
    with pytest.raises(engine.EngineError, match='EXIT_TAGS'):
        monitoring.run({'EXIT_TAGS': ''}, docker=engine.Docker(Run(), tmp_path / 'c.yaml'))


def runtime(tmp_path):
    return json.loads((tmp_path / 'telegram-runtime.json').read_text())


def test_bot_settings_follow_config_changes_but_keep_panel_edits(tmp_path):
    env = {'TELEGRAM_BOT_TOKEN': '1:a', 'TELEGRAM_CHAT_ID': '-1', 'TELEGRAM_ADMIN_USERNAME': 'boss'}
    assert telegram.sync_runtime(env, [], tmp_path) is True
    assert runtime(tmp_path) == {'bot_token': '1:a', 'chat_id': '-1', 'admin_username': 'boss'}
    assert telegram.sync_runtime(env, [], tmp_path) is False
    # Панель сменила чат: повтор деплоя с тем же config.yaml его не затирает.
    (tmp_path / 'telegram-runtime.json').write_text(json.dumps({**runtime(tmp_path), 'chat_id': '-2'}))
    assert telegram.sync_runtime(env, [], tmp_path) is False and runtime(tmp_path)['chat_id'] == '-2'
    # Токен сменился в config.yaml - применяется; чат панели остаётся.
    assert telegram.sync_runtime({**env, 'TELEGRAM_BOT_TOKEN': '2:b'}, [], tmp_path) is True
    assert runtime(tmp_path) == {'bot_token': '2:b', 'chat_id': '-2', 'admin_username': 'boss'}
    # Токен снят в config.yaml и панелью не менялся - очищается; чат панели и админ остаются.
    assert telegram.sync_runtime({}, [], tmp_path) is True
    assert runtime(tmp_path) == {'bot_token': '', 'chat_id': '-2', 'admin_username': 'boss'}


def test_bot_settings_are_created_through_dangling_link(tmp_path):
    """Роль control ставит ссылку на настройки бота до первой стадии бота: цели ещё нет."""
    target = tmp_path / 'panel' / 'telegram-runtime.json'
    target.parent.mkdir()
    link = tmp_path / 'telegram-runtime.json'
    link.symlink_to(target)
    assert telegram.sync_runtime({'TELEGRAM_CHAT_ID': '-1'}, [], tmp_path) is True
    assert link.is_symlink() and json.loads(target.read_text())['chat_id'] == '-1'
    assert target.stat().st_mode & 0o777 == 0o600


def test_bot_settings_of_old_install_come_from_container_env(tmp_path):
    """Установка без telegram-from-config.json: прошлые значения берутся из env старого бота."""
    (tmp_path / 'telegram-runtime.json').write_text(json.dumps({'bot_token': 'ui', 'chat_id': '-1'}))
    old = ['TELEGRAM_BOT_TOKEN=1:a', 'TELEGRAM_CHAT_ID=-1', 'PATH=/bin']
    assert telegram.sync_runtime({'TELEGRAM_BOT_TOKEN': '1:a', 'TELEGRAM_CHAT_ID': '-1'}, old, tmp_path) is False
    assert runtime(tmp_path) == {'bot_token': 'ui', 'chat_id': '-1'}


def test_bot_secret_is_created_once(tmp_path):
    value, created = telegram.secret(tmp_path)
    assert created and len(value) == 32 and (tmp_path / 'tgbot-secret.txt').stat().st_mode & 0o777 == 0o600
    assert telegram.secret(tmp_path) == (value, False)
    (tmp_path / 'tgbot-secret.txt').write_text('')
    assert telegram.secret(tmp_path)[1] is True


def test_panel_secret_reads_running_panel():
    panel = {'Image': 'sha256:p', 'Config': {'Env': ['NUXT_TGBOT_SECRET=abc', 'X=1']}}
    assert telegram.panel_secret(engine.Docker(Run({compose.PANEL: panel}))) == ('abc', 'sha256:p')
    assert telegram.panel_secret(engine.Docker(Run())) == (None, '')


@pytest.mark.parametrize('body', ['', '{bro', 'null', '[]'])
def test_broken_record_of_config_values_falls_back_to_bot_env(tmp_path, body):
    env = {'TELEGRAM_BOT_TOKEN': '1:a', 'TELEGRAM_CHAT_ID': '100'}
    old = ['TELEGRAM_BOT_TOKEN=1:a', 'TELEGRAM_CHAT_ID=100']
    taken = tmp_path / 'telegram-from-config.json'
    telegram.sync_runtime(env, [], tmp_path)
    # Бота нет: прошлого не знаем, токен остаётся.
    taken.write_text(body)
    telegram.sync_runtime({}, [], tmp_path)
    assert runtime(tmp_path)['bot_token'] == '1:a' and runtime(tmp_path)['chat_id'] == '100'
    assert json.loads(taken.read_text()) == {'bot_token': '', 'chat_id': '', 'admin_username': ''}
    assert taken.stat().st_mode & 0o777 == 0o600
    # Бот с токеном config.yaml есть: снятый в config.yaml токен выключается.
    taken.write_text(body)
    telegram.sync_runtime({}, old, tmp_path)
    assert runtime(tmp_path)['bot_token'] == '' and runtime(tmp_path)['chat_id'] == ''
    # Токен заменён в панели: деплой без telegram его не трогает.
    (tmp_path / 'telegram-runtime.json').write_text(json.dumps({'bot_token': 'ui', 'chat_id': '200'}))
    taken.write_text(body)
    telegram.sync_runtime({}, old, tmp_path)
    assert runtime(tmp_path) == {'bot_token': 'ui', 'chat_id': '200'}


def test_alert_thresholds_reach_bot_only_when_set():
    text = 'ENTRY_DOCKER_ARGS=()\nENTRY_RUNTIME_ARGS=()\n'
    alerts = {'ALERT_CPU_PCT': '85', 'ALERT_RAM_PCT': '90', 'ALERT_WINDOW_SEC': '120'}
    for environ in (alerts, {}):
        env = compose.bot_service(environ, 'i', text, secret='s', user='1:1', panel_gid=2)['environment']
        assert {name: value for name, value in env.items() if name.startswith('ALERT_')} == environ
    # Бот ставится и без токена: пустые поля, секрет на месте.
    assert (env['TELEGRAM_BOT_TOKEN'], env['TELEGRAM_CHAT_ID'], env['TGBOT_SECRET']) == ('', '', 's')


@pytest.fixture
def bot_stage(tmp_path, monkeypatch):
    stamps, user = [], type('User', (), {'pw_uid': 993, 'pw_gid': 992, 'gr_gid': 991})()
    monkeypatch.setattr(telegram, 'ETC', str(tmp_path))
    real = telegram.secret
    monkeypatch.setattr(telegram, 'secret', lambda: real(tmp_path))
    monkeypatch.setattr(telegram, 'sync_runtime', lambda *_args: False)
    monkeypatch.setattr(telegram, 'place_runtime', lambda: None)
    monkeypatch.setattr(telegram.pwd, 'getpwnam', lambda _name: user)
    monkeypatch.setattr(telegram.grp, 'getgrnam', lambda _name: user)
    monkeypatch.setattr(engine, 'release_image', lambda *_args, **_kwargs: 'sha256:bot')
    monkeypatch.setattr(engine, 'resource_text', lambda *_args: 'ENTRY_DOCKER_ARGS=()\nENTRY_RUNTIME_ARGS=()\n')
    monkeypatch.setattr(engine, 'drop_timezone_dir', lambda: None)
    monkeypatch.setattr(engine, 'stamp', stamps.append)

    def run(containers, environ=(), panel_stage=None):
        docker = engine.Docker(Run(containers), tmp_path / 'compose.yaml')
        changed = telegram.run(dict(environ), docker=docker, say=lambda _text: None,
                               panel_stage=panel_stage, sleep=lambda _pause: None)
        return changed, docker

    return run, stamps, tmp_path


def test_panel_is_recreated_when_it_holds_another_bot_secret(bot_stage):
    run, stamps, etc = bot_stage
    panel = {compose.PANEL: {'Id': 'p', 'Image': 'sha256:p', 'Config': {'Env': ['NUXT_TGBOT_SECRET=old']}}}
    asked = []
    changed, docker = run(panel, panel_stage=asked.append)
    secret = (etc / 'tgbot-secret.txt').read_text()
    assert changed and len(asked) == 1 and asked[0]['PANEL_IMAGE_PULL'] == 'false'
    assert docker.services()[compose.BOT]['environment']['TGBOT_SECRET'] == secret
    assert stamps == [telegram.STAGE]
    # Полная установка (стадия 30 идёт следом), панель с тем же секретом и узел без панели её не трогают.
    same = {compose.PANEL: {'Id': 'p', 'Image': 'i', 'Config': {'Env': [f'NUXT_TGBOT_SECRET={secret}']}}}
    for containers, environ in ((panel, {'VPN2_PANEL_FOLLOWS': 'true'}), (same, {}), ({}, {})):
        run(containers, environ, panel_stage=asked.append)
    assert len(asked) == 1


def test_failed_panel_stops_bot_stage(bot_stage):
    run, stamps, _ = bot_stage
    panel = {compose.PANEL: {'Id': 'p', 'Image': 'sha256:p', 'Config': {'Env': ['NUXT_TGBOT_SECRET=old']}}}

    def broken(_environ):
        raise engine.EngineError('панель не поднялась')

    with pytest.raises(engine.EngineError):
        run(panel, panel_stage=broken)
    assert stamps == []
