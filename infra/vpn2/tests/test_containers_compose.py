"""Службы compose против эталона: argv `docker run` снят с bash-стадий 25, 30 и 35 до их удаления."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vpn2.containers import compose

RUNS = json.loads((Path(__file__).parent / 'fixtures/containers/docker-run.json').read_text())
# Окружение, на котором снят эталон.
ENV = {
    'HOST_TAG': 'ru', 'ENTRY_HOST': '198.51.100.7', 'AGH_PORT': '3000', 'MGMT_IP': '10.99.0.1',
    'EXIT_TAGS': 'de us-east', 'DOMAIN_DE': '203.0.113.5', 'DOMAIN_US_EAST': 'us.example.org',
    'MGMT_IP_DE': '10.99.0.2', 'MGMT_IP_US_EAST': '10.99.0.3', 'WG_MTU': '1380', 'OVPN_PROTO': 'tcp',
    'SSO_LABEL': 'Вход', 'SSO_ALLOWED_SUBS': 'a,b', 'SSO_CLIENT_ID': 'cid', 'SSO_CLIENT_SECRET': 's$1',
    'SSO_DISCOVERY_URL': 'https://sso.example.org/.well-known',
    'TELEGRAM_BOT_TOKEN': '123:abc', 'TELEGRAM_CHAT_ID': '-100', 'TELEGRAM_ADMIN_USERNAME': 'boss',
    'ALERT_CPU_PCT': '90', 'TGBOT_INVALID_TOKEN_PAUSE_SEC': '5',
}
FACTS = {
    'session': 'sess', 'clash': 'clash$ecret', 'tgbot': 'bot-secret', 'admin_user': 'owner',
    'agh_password': 'agh-p$a"ss', 'sso_enabled': 'true', 'sso_auto_redirect': 'true',
    'sso_password_login': 'false', 'sso_redirect_url': 'https://panel.example.org/auth/authentik',
    'previous_node_hosts': 'ru=198.51.100.1',
}
RESOURCES = {
    'panel': 'ENTRY_DOCKER_ARGS=(--memory 192m --memory-swap 192m -e NODE_OPTIONS=--max-old-space-size=128)\n'
             'ENTRY_RUNTIME_ARGS=()\n',
    'bot': 'ENTRY_DOCKER_ARGS=(--memory 64m --memory-swap 64m)\nENTRY_RUNTIME_ARGS=()\n',
    'vm': 'ENTRY_DOCKER_ARGS=(--memory 128m --memory-swap 128m)\n'
          'ENTRY_RUNTIME_ARGS=(-retentionPeriod=1d -memory.allowedBytes=32MiB '
          '-search.maxMemoryPerQuery=8MiB -search.maxConcurrentRequests=2)\n',
}
SINGLE = {'--name': 'container_name', '--memory': 'mem_limit', '--memory-swap': 'memswap_limit',
          '--restart': 'restart', '--network': 'network_mode', '--user': 'user'}
MANY = {'--group-add': 'group_add', '--cap-drop': 'cap_drop', '--security-opt': 'security_opt',
        '-v': 'volumes'}


def from_run(argv):
    """Разобрать argv `docker run -d ...` в поля службы compose."""
    assert argv[:2] == ['run', '-d']
    service, words = {'environment': {}, 'logging': {'options': {}}, 'labels': {}}, iter(argv[2:])
    for flag in words:
        if not flag.startswith('-'):
            service['image'] = flag
            break
        value = next(words)
        if flag in SINGLE:
            service[SINGLE[flag]] = value
        elif flag in MANY:
            service.setdefault(MANY[flag], []).append(value)
        elif flag in ('-e', '--label', '--log-opt'):
            name, _, text = value.partition('=')
            target = {'-e': service['environment'], '--label': service['labels'],
                      '--log-opt': service['logging']['options']}[flag]
            target[name] = text
        elif flag == '--log-driver':
            service['logging']['driver'] = value
        else:
            raise AssertionError(f'неизвестный флаг эталона: {flag}')
    command = list(words)
    if command:
        service['command'] = command
    return service


def plain(service):
    """Метки в эталон не входят: отпечаток панели считается иначе, у VM метки не было."""
    return {key: value for key, value in service.items() if key != 'labels' and value != {}}


def services():
    return {
        compose.PANEL: compose.panel_service(ENV, 'sha256:panel', RESOURCES['panel'], FACTS,
                                             user='990:991', control_gid=989),
        compose.BOT: compose.bot_service(ENV, 'sha256:bot', RESOURCES['bot'], secret='bot-secret',
                                         user='993:992', panel_gid=991),
        compose.VM: compose.vm_service('victoriametrics/victoria-metrics:v1.155.0', RESOURCES['vm'],
                                       compose.scrape_config(ENV)[0]),
    }


@pytest.mark.parametrize('name', [compose.PANEL, compose.BOT, compose.VM])
def test_service_matches_docker_run_of_bash_stage(name):
    assert plain(services()[name]) == plain(from_run(RUNS[name]))


def test_env_order_of_panel_is_kept():
    """Порядок env виден в docker inspect: он тот же, что у bash-стадии."""
    assert list(services()[compose.PANEL]['environment']) == list(from_run(RUNS[compose.PANEL])['environment'])


def test_difference_from_etalon_is_caught():
    """Отрицательный контроль: потерянный том, env или право не проходят сверку."""
    for name, damage in ((compose.PANEL, lambda s: s['volumes'].pop()),
                         (compose.BOT, lambda s: s['environment'].pop('TG_PROXY')),
                         (compose.VM, lambda s: s['command'].remove('-search.latencyOffset=1s')),
                         (compose.PANEL, lambda s: s.pop('cap_drop')),
                         (compose.BOT, lambda s: s.update(user='0:0'))):
        service = services()[name]
        damage(service)
        assert plain(service) != plain(from_run(RUNS[name]))


def test_file_keeps_other_services_and_reports_change():
    text, changed = compose.with_service('', compose.BOT, services()[compose.BOT])
    assert changed
    again, changed = compose.with_service(text, compose.BOT, services()[compose.BOT])
    assert (again, changed) == (text, False)
    both, changed = compose.with_service(text, compose.VM, services()[compose.VM])
    assert changed and list(compose.load(both)) == [compose.VM, compose.BOT]
    assert compose.load(both)[compose.BOT] == compose.load(text)[compose.BOT]
    assert compose.unescape(compose.load(both)[compose.BOT]) == services()[compose.BOT]


def test_spec_ignores_previous_hosts_and_follows_image_and_config():
    service = services()[compose.PANEL]
    spec = compose.panel_spec(service, b'config')
    moved = compose.panel_service(ENV, 'sha256:panel', RESOURCES['panel'],
                                  {**FACTS, 'previous_node_hosts': ''}, user='990:991', control_gid=989)
    assert compose.panel_spec(moved, b'config') == spec
    assert compose.panel_spec({**service, 'image': 'sha256:other'}, b'config') != spec
    assert compose.panel_spec(service, b'other') != spec


def test_previous_hosts_prefer_saved_map():
    assert compose.previous_node_hosts(['NUXT_NODE_HOSTS=ru=1', 'NUXT_PREVIOUS_NODE_HOSTS=ru=0']) == 'ru=0'
    assert compose.previous_node_hosts(['NUXT_NODE_HOSTS=ru=1', 'NUXT_PREVIOUS_NODE_HOSTS=']) == 'ru=1'
    assert compose.previous_node_hosts([]) == ''


def test_docker_compose_reads_file_back_unchanged(tmp_path):
    """Сам compose разбирает файл в те же значения: `$` в секретах не подстановка."""
    if not shutil.which('docker') or subprocess.run(['docker', 'compose', 'version'],
                                                    capture_output=True, check=False).returncode:
        pytest.skip('docker compose is absent')
    text = ''
    for name, service in services().items():
        text, _ = compose.with_service(text, name, service)
    path = tmp_path / 'compose.yaml'
    path.write_text(text)
    resolved = subprocess.run(['docker', 'compose', '-f', str(path), 'config', '--format', 'json'],
                              capture_output=True, text=True, check=True, env={'PATH': '/usr/bin:/bin'})
    read = json.loads(resolved.stdout)
    assert read['name'] == compose.PROJECT
    for name, service in services().items():
        got = read['services'][name]
        want = {key: str(value) for key, value in service['environment'].items()} \
            if 'environment' in service else {}
        assert {key: value.replace('$$', '$') for key, value in got.get('environment', {}).items()} == want
        assert got['container_name'] == name and got['network_mode'] == 'host'
        assert got.get('command') == (
            [word.replace('$', '$$') for word in service['command']] if 'command' in service else None)
