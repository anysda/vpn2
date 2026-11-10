"""Шаги роли adguard: эталоны сняты с bash-стадии 22-adguard до её удаления."""

import hashlib
import ipaddress
import json

import pytest
import yaml

from vpn2 import svc
from vpn2.adguard import dns, stage

ENV = {'AGH_PORT': '3000', 'MGMT_IP': '10.99.0.1', 'WG_SERVER_IP': '10.66.66.1'}
POOLS = {'wg': {'subnets': ['10.66.66.0/29']}, 'ovpn': {'subnets': ['10.67.67.0/30']},
         'ikev2': {'subnets': ['10.68.68.0/30']}, 'other': {'subnets': ['10.1.0.0/30']}}
HASH = '$2a$10$abc'
# sha256 файлов, которые стадия 22 писала на тех же входах (конфиг без фильтров
# и состояние не зависят от пулов адресов).
STAGE_PLAIN = {'fresh': 'c5c80777ed7b6b1338ff46dac9553621eb3cd27adaa26bd8366e4f8f6eb7e80b',
               'legacy': '71d036ccc79786f5d2881c3330b2840bbc4ec53c5c90feda68636698b168371b'}
STAGE_STATE = '7b27edfe886e60e391b0f470787b1fce49915b8baae5e69c7b424f91f80c3d75'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def host(tmp_path):
    for name in ('agh', 'plain', 'etc', 'work'):
        (tmp_path / name).mkdir()
    (tmp_path / 'pools.json').write_text(json.dumps(POOLS))

    def build(user='anysda', digest=HASH):
        return stage.build_configs(
            ENV, user, 'pw', agh_dir=tmp_path / 'agh', plain_dir=tmp_path / 'plain', etc=tmp_path / 'etc',
            pools_file=tmp_path / 'pools.json', work=tmp_path / 'work', hasher=lambda *_: digest,
            profile=lambda _env: {'AGH_ADAWAY': 'true', 'AGH_QUERYLOG': '500'}, patch=lambda *_: None)

    def publish(staged):
        for path, name in zip(staged, ('agh', 'plain')):
            stage.publish(name, path, tmp_path / name / 'AdGuardHome.yaml', active=lambda _unit: False,
                          forget=lambda _unit: None)

    return tmp_path, build, publish


def test_fresh_install_matches_stage_files(host):
    root, build, _ = host
    main, plain = build()
    cfg = yaml.safe_load(main.read_text())
    assert cfg['http'] == {'address': '127.0.0.1:3000'}
    assert cfg['users'] == [{'name': 'anysda', 'password': HASH}]
    assert cfg['schema_version'] == 28
    assert {key: cfg['dns'][key] for key in dns.DEFAULTS} == dns.DEFAULTS
    assert cfg['dns']['bind_hosts'] == ['127.0.0.1', '10.99.0.1']
    assert cfg['dns']['edns_client_subnet']['enabled'] is False
    assert [item['enabled'] for item in cfg['filters']] == [True, True]
    assert cfg['querylog'] == {'size_memory': 500}
    nets = ['10.66.66.0/29', '10.67.67.0/30', '10.68.68.0/30', '10.66.66.1/24', '10.67.67.0/24', '10.68.68.0/24']
    hosts = dict.fromkeys(str(ip) for net in nets for ip in ipaddress.ip_network(net, strict=False).hosts())
    assert cfg['dns']['ratelimit_whitelist'] == list(hosts)
    assert sha(plain) == STAGE_PLAIN['fresh']
    assert sha(root / 'etc/agh-dns.json') == STAGE_STATE
    assert main.stat().st_mode & 0o777 == plain.stat().st_mode & 0o777 == 0o600


def test_repeat_keeps_both_texts(host):
    root, build, publish = host
    publish(build())
    first = [(root / name / 'AdGuardHome.yaml').read_bytes() for name in ('agh', 'plain')]
    assert [path.read_bytes() for path in build()] == first


def test_panel_choice_survives_and_deploy_value_follows(host):
    root, build, publish = host
    publish(build())
    live = root / 'agh/AdGuardHome.yaml'
    cfg = yaml.safe_load(live.read_text())
    cfg['dns'].update(upstream_dns=['9.9.9.9'], ratelimit=50)
    cfg['http']['address'] = '0.0.0.0:3000'
    live.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    main, plain = build(digest='$2a$10$new')
    after = yaml.safe_load(main.read_text())
    assert after['dns']['upstream_dns'] == ['9.9.9.9'] and after['dns']['ratelimit'] == 50
    assert after['http']['address'] == '127.0.0.1:3000'
    assert after['users'][0]['password'] == '$2a$10$new'
    unfiltered = yaml.safe_load(plain.read_text())
    assert unfiltered['dns']['upstream_dns'] == ['9.9.9.9'] and unfiltered['dns']['ratelimit'] == 0
    assert unfiltered['users'] == after['users']
    # Состояние помнит значение деплоя, а не выбор панели.
    assert sha(root / 'etc/agh-dns.json') == STAGE_STATE


def test_hub_of_earlier_releases_without_state(host):
    root, build, publish = host
    publish(build())
    (root / 'etc/agh-dns.json').unlink()
    (root / 'plain/AdGuardHome.yaml').unlink()
    live = root / 'agh/AdGuardHome.yaml'
    cfg = yaml.safe_load(live.read_text())
    cfg['dns'].update(bootstrap_dns=['77.88.8.8', '8.8.8.8'], cache_optimistic=False, ratelimit=20,
                      upstream_dns=['77.88.8.8', '[/youtube.com/]1.1.1.1'], aaaa_disabled=False)
    live.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    main, plain = build()
    after = yaml.safe_load(main.read_text())['dns']
    assert {key: after[key] for key in dns.DEFAULTS} == {**dns.DEFAULTS, 'aaaa_disabled': False}
    assert sha(plain) == STAGE_PLAIN['legacy']
    assert sha(root / 'etc/agh-dns.json') == STAGE_STATE


def test_unfiltered_keeps_adguard_additions_and_schema(host):
    root, build, publish = host
    publish(build())
    live = root / 'plain/AdGuardHome.yaml'
    cfg = yaml.safe_load(live.read_text())
    cfg['schema_version'] = 33
    cfg['dns']['own'] = True
    live.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    assert build()[1].read_bytes() == live.read_bytes()


def test_publish_stops_only_changed_instance(tmp_path):
    staged, target = tmp_path / 'staged', tmp_path / 'live/AdGuardHome.yaml'
    staged.write_text('a: 1\n')
    calls = []
    kwargs = {'active': lambda _unit: True, 'stop': lambda unit: calls.append(('stop', unit)),
              'forget': lambda unit: calls.append(('forget', unit))}
    assert stage.publish('adguardhome', staged, target, **kwargs)
    assert calls == [('stop', 'adguardhome'), ('forget', 'adguardhome')]
    assert target.stat().st_mode & 0o777 == 0o600
    assert not stage.publish('adguardhome', staged, target, **kwargs)
    assert len(calls) == 2


def test_credentials_files_and_long_password(tmp_path):
    user, password, changed = stage.credentials({'ADMIN_PASSWORD': 'p' * 80, 'ADMIN_USER': 'boss'}, tmp_path)
    # printf 'vpn2-adguard-v1\0pppp…' | sha256sum, как agh_password в anysda-svc.sh
    assert password == hashlib.sha256(b'vpn2-adguard-v1\0' + b'p' * 80).hexdigest()
    assert (user, changed) == ('boss', True)
    assert (tmp_path / 'admin-password.txt').read_text() == 'p' * 80
    assert (tmp_path / 'adguard-password.txt').stat().st_mode & 0o777 == 0o600
    assert stage.credentials({'ADMIN_PASSWORD': 'p' * 80}, tmp_path) == ('anysda', password, False)
    assert stage.agh_password('короткий') == 'короткий'


def test_generated_password_is_kept(tmp_path):
    _, password, changed = stage.credentials({}, tmp_path)
    assert changed and len(password) == 18 and password.isalnum()
    assert stage.credentials({}, tmp_path) == ('anysda', password, False)


def test_hash_from_config_is_reused_when_it_fits():
    cfg = {'users': [{'name': 'anysda', 'password': 'old'}]}
    assert stage.password_hash(cfg, 'anysda', 'pw', matches=lambda *_: True, create=lambda _p: 'new') == 'old'
    assert stage.password_hash(cfg, 'anysda', 'pw', matches=lambda *_: False, create=lambda _p: 'new') == 'new'
    assert stage.password_hash(None, 'anysda', 'pw', matches=lambda *_: True, create=lambda _p: 'new') == 'new'


def test_fingerprint_file_format_is_shared_with_shell_library(tmp_path):
    (tmp_path / 'a').write_text('one\n')
    # printf 'one\n' | sha256sum
    assert svc.fingerprint([tmp_path / 'a', tmp_path / 'missing']) == (
        '2c8b08da5ce60398e1f19af0e5dccc744df274b826abe585eaba68c525434806')
    (tmp_path / 'unit.sha256').write_text('x\n')
    svc.forget('unit', tmp_path)
    assert not (tmp_path / 'unit.sha256').exists()
