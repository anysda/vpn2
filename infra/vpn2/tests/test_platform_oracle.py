"""Выходы ресурсов, compose.yaml и конфигов AdGuard равны снятым с прежней реализации (fixtures)."""
import contextlib
import hashlib
import io
import json
import shlex
from pathlib import Path

import pytest
import yaml

from vpn2 import config, resources
from vpn2.adguard import dns, stage
from vpn2.containers import compose

FIXTURES = Path(__file__).parent / 'fixtures'
RES = json.loads((FIXTURES / 'resources.json').read_text())
COMPOSE = json.loads((FIXTURES / 'compose.json').read_text())
ADGUARD = json.loads((FIXTURES / 'adguard.json').read_text())


def _values(key):
    ram, free, overrides = key.split('/', 2)
    return resources.calculate(int(ram), int(free), json.loads(overrides))


@pytest.mark.parametrize('ram', sorted(RES['units']))
def test_unit_dropins(ram):
    for unit, body in RES['units'][ram].items():
        assert resources.unit_dropin(int(ram), unit) == body, unit


@pytest.mark.parametrize('key', sorted(RES['values']))
def test_budgets_and_service_limits(key):
    values = _values(key)
    assert values == RES['values'][key]
    for service, text in RES['services'][key].items():
        old = {name: shlex.split(value[1:-1]) if value.startswith('(') else shlex.split(value)[0]
               for name, _, value in (line.partition('=') for line in text.splitlines())}
        if service == 'agh':
            assert resources.adguard(values) == {k: v for k, v in old.items() if k.startswith('AGH_')}
            continue
        limits, env, runtime = resources.container(values, service)
        docker = [*(item for key_, flag in (('mem_limit', '--memory'), ('memswap_limit', '--memory-swap'))
                    if key_ in limits for item in (flag, limits[key_])),
                  *(item for name, value in env.items() for item in ('-e', f'{name}={value}'))]
        assert (docker, runtime) == (old['ENTRY_DOCKER_ARGS'], old['ENTRY_RUNTIME_ARGS'])


def test_bad_override_is_refused_by_config():
    for line in ('  retention_days: 0', '  unknown: 1', '  adaway_enabled: maybe'):
        with pytest.raises(config.ConfigError):
            config.loads(f'entry_resources:\n{line}\n')


@pytest.mark.parametrize('key', sorted(COMPOSE['cases']))
def test_compose_file_and_panel_spec(key):
    case, env, facts = COMPOSE['cases'][key], COMPOSE['env'], COMPOSE['facts']
    values = _values(key)
    scrape, skipped = compose.scrape_config(env)
    services = {compose.VM: compose.vm_service('vm:1', values, scrape),
                compose.BOT: compose.bot_service(env, 'bot@sha256:1', values, secret='x', user='999:998', panel_gid=997),
                compose.PANEL: compose.panel_service(env, 'panel@sha256:2', values, facts, user='996:995', control_gid=994)}
    text = ''
    for name, service in services.items():
        text, _ = compose.with_service(text, name, service)
    assert text == case['compose']
    assert compose.with_service(text, compose.PANEL, services[compose.PANEL]) == (text, False)
    assert compose.load(text)[compose.PANEL] == services[compose.PANEL]
    assert compose.panel_spec(services[compose.PANEL], b'config: 1\n') == case['spec']
    assert (scrape, skipped) == (case['scrape'], case['skipped'])


def _legacy(root):
    cfg = yaml.safe_load(dns.fresh_text('anysda', 'OLD', '3000', '10.99.0.1', 'false', '100'))
    cfg['dns'].update(bootstrap_dns=['77.88.8.8', '8.8.8.8'], ratelimit=20, cache_optimistic=False,
                      upstream_dns=['77.88.8.8', '[/youtube.com/googlevideo.com/]1.2.3.4'], ratelimit_whitelist=['10.66.66.1'])
    (root / 'agh/AdGuardHome.yaml').write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))


def _panel(root):
    cfg = yaml.safe_load(dns.fresh_text('anysda', 'HASH-pw', '3000', '10.99.0.1', 'false', '100'))
    cfg['dns'].update(upstream_dns=['9.9.9.9'], aaaa_disabled=False)
    cfg['querylog']['size_memory'] = 33
    cfg['filters'][1]['enabled'] = True
    (root / 'agh/AdGuardHome.yaml').write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    (root / 'etc/agh-dns.json').write_text(json.dumps({'upstream_dns': dns.DEFAULTS['upstream_dns'], 'aaaa_disabled': True}))
    (root / 'etc/agh-resources.json').write_text(json.dumps({'querylog_size': 100, 'adaway_enabled': False}))
    (root / 'plain/AdGuardHome.yaml').write_text('schema_version: 33\nclients:\n  runtime_sources: {whois: true}\n'
                                                 'statistics: {interval: 24h}\n')


SCENARIOS = {'fresh': (None, ''), 'fresh_overrides': (None, '{"querylog_size": "7", "adaway_enabled": "true"}'),
             'legacy': (_legacy, ''), 'panel': (_panel, '')}


@pytest.mark.parametrize('name', sorted(SCENARIOS))
def test_adguard_configs(name, tmp_path, monkeypatch):
    """Новая установка, ресурсы из config.yaml, хаб прежних выпусков, правки панели; второй прогон - тот же текст."""
    monkeypatch.setattr(resources, 'hardware', lambda: (2048, 8192))
    monkeypatch.setattr(stage, 'password_hash', lambda _cfg, _user, password: 'HASH-' + password)
    setup, overrides = SCENARIOS[name]
    for sub in ('agh', 'plain', 'etc', 'work'):
        (tmp_path / sub).mkdir()
    pools = tmp_path / 'pools.json'
    pools.write_text(json.dumps({kind: {'subnets': [subnet]} for kind, subnet in (
        ('wg', '10.66.66.0/30'), ('ovpn', '10.67.67.0/30'), ('ikev2', '10.68.68.0/30'))}))
    if setup:
        setup(tmp_path)
    env = {'AGH_PORT': '3000', 'MGMT_IP': '10.99.0.1', 'ENTRY_RESOURCE_OVERRIDES': overrides}
    for expected in ADGUARD[name]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            staged = stage.build_configs(env, 'anysda', 'pw', tmp_path / 'work', agh_dir=tmp_path / 'agh',
                                         plain_dir=tmp_path / 'plain', etc=tmp_path / 'etc', pools=pools)
        for path, live in zip(staged, ('agh', 'plain')):
            (tmp_path / live / 'AdGuardHome.yaml').write_bytes(path.read_bytes())
        files = {name_: hashlib.sha256((tmp_path / name_).read_bytes()).hexdigest() for name_ in expected['files']}
        assert (out.getvalue(), err.getvalue(), files) == (expected['out'], expected['err'], expected['files'])


NODEVARS = json.loads((FIXTURES / 'nodevars.json').read_text())


@pytest.mark.parametrize('name', sorted(NODEVARS))
def test_node_env(name, tmp_path):
    from nodevars_cases import cases
    from vpn2 import config, nodevars
    text, state = cases()[name]
    for file, body in state.items():
        (tmp_path / file).write_text(body)
    try:
        result = nodevars.env_by_node(config.loads(text), state=tmp_path, development=name == 'development')
    except config.ConfigError as error:
        result = 'ConfigError: ' + str(error)
    assert result == NODEVARS[name]
