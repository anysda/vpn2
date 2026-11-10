"""Слепок: содержимое конфигов с секретами сверяется без секретов (shape), каталоги данных - без содержимого."""
import json

import pytest

from vpn2 import snapshot
from vpn2.snapshot import diff

WORD = 'pass' + 'word'


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, '_run', lambda *_: (127, ''))
    for name in ('etc/sing-box', 'etc/wireguard', 'etc/anysda', 'etc/vpn2-control', 'opt/AdGuardHome/data',
                 'opt/AdGuardHome-unfiltered', 'var/lib/sing-box', 'var/lib/caddy/certs'):
        (tmp_path / name).mkdir(parents=True)
    return tmp_path


def files(root):
    return snapshot.collect(root)['files']


def singbox(root, secret, port=443):
    config = {'inbounds': [{'type': 'vless', 'listen_port': port, 'users': [{'name': 'a', 'uuid': secret}],
                            'tls': {'reality': {'private_key': secret, 'short_id': [secret]}}}],
              'experimental': {'clash_api': {'secret': secret}}}
    (root / 'etc/sing-box/config.json').write_text(json.dumps(config, indent=2))


def test_other_secrets_same_shape(root):
    singbox(root, 'one')
    before = files(root)['/etc/sing-box/config.json']
    singbox(root, 'two')
    after = files(root)['/etc/sing-box/config.json']
    assert before['sha256'] != after['sha256']
    assert before['shape'] == after['shape']


def test_extra_inbound_port_changes_shape(root):
    singbox(root, 'one')
    old = snapshot.collect(root)
    singbox(root, 'one', port=8443)
    lines = diff(old, snapshot.collect(root),
                 [{'section': 'files', 'name': '/etc/sing-box/config*.json', 'field': 'sha256', 'reason': '-'}])
    assert len(lines) == 1 and lines[0].startswith('files /etc/sing-box/config.json shape: ')


@pytest.mark.parametrize('name, text, other, changed', [
    ('etc/wireguard/wg0.conf', '[Interface]\nPrivateKey = aaa\nListenPort = 51820\n',
     '[Interface]\nPrivateKey = bbb\nListenPort = 51820\n', '[Interface]\nPrivateKey = aaa\nListenPort = 51821\n'),
    ('etc/anysda/failover-watchdog.env', 'CLASH_SECRET=aaa\nINTERVAL=5\n',
     'CLASH_SECRET="b b"\nINTERVAL=5\n', 'CLASH_SECRET=aaa\nINTERVAL=6\n'),
    ('etc/anysda/anysda-config.yaml', f'admin:\n  - name: root\n    {WORD}: "aaa"\nport: 1\n',
     f'admin:\n  - name: root\n    {WORD}: "bbb"\nport: 1\n', f'admin:\n  - name: toor\n    {WORD}: "aaa"\nport: 1\n'),
    ('etc/anysda/anysda-config.yaml', 'ssh_key: |\n  line one\n  line two\nport: 1\n',
     'ssh_key: |\n  other\nport: 1\n', 'ssh_key: |\n  line one\n  line two\nport: 2\n'),
    ('opt/AdGuardHome-unfiltered/AdGuardHome.yaml', f'users:\n  - name: a\n    {WORD}: $2a$10$aaa\ndns:\n  port: 53\n',
     f'users:\n  - name: a\n    {WORD}: $2a$10$bbb\ndns:\n  port: 53\n',
     f'users:\n  - name: a\n    {WORD}: $2a$10$aaa\ndns:\n  port: 54\n'),
])
def test_line_configs(root, name, text, other, changed):
    (root / name).write_text(text)
    first = files(root)['/' + name]['shape']
    (root / name).write_text(other)
    assert files(root)['/' + name]['shape'] == first
    (root / name).write_text(changed)
    assert files(root)['/' + name]['shape'] != first


def test_accounts_without_numbers(root):
    (root / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/bash\nvpn2-panel:x:998:997::/nonexistent:/usr/sbin/nologin\n')
    (root / 'etc/group').write_text('root:x:0:\nvpn2-control:x:996:vpn2-panel\n')
    (root / 'etc/vpn2-control/callers.json').write_text('[0, 998]')
    first = files(root)
    (root / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/bash\nvpn2-panel:x:110:115::/nonexistent:/usr/sbin/nologin\n')
    (root / 'etc/group').write_text('root:x:0:\nvpn2-control:x:113:vpn2-panel\n')
    (root / 'etc/vpn2-control/callers.json').write_text('[0, 110]')
    second = files(root)
    for name in ('/etc/passwd', '/etc/group', '/etc/vpn2-control/callers.json'):
        assert first[name]['sha256'] != second[name]['sha256']
        assert first[name]['shape'] == second[name]['shape'], name
    (root / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/bash\nvpn2-panel:x:110:115::/nonexistent:/bin/bash\n')
    (root / 'etc/group').write_text('root:x:0:\nvpn2-control:x:113:vpn2-panel,vpn2-bot\n')
    (root / 'etc/vpn2-control/callers.json').write_text('[110]')
    third = files(root)
    for name in ('/etc/passwd', '/etc/group', '/etc/vpn2-control/callers.json'):
        assert third[name]['shape'] != second[name]['shape'], name


def test_plain_file_has_no_shape(root):
    (root / 'etc/hosts').write_text('127.0.0.1 localhost\n')
    assert 'shape' not in files(root)['/etc/hosts']


def test_broken_callers_is_an_error_not_a_crash(root):
    (root / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/bash\n')
    (root / 'etc/vpn2-control/callers.json').write_text('{')
    assert files(root)['/etc/vpn2-control/callers.json']['error'].startswith('shape')


def test_adguard_config_and_binary_without_data(root):
    (root / 'opt/AdGuardHome/AdGuardHome').write_bytes(b'\x7fELF')
    (root / 'opt/AdGuardHome/AdGuardHome.yaml').write_text('dns:\n  port: 53\n')
    (root / 'opt/AdGuardHome/data/querylog.json').write_text('{}')
    names = {name for name in files(root) if name.startswith('/opt/AdGuardHome')}
    assert names == {'/opt/AdGuardHome', '/opt/AdGuardHome/AdGuardHome', '/opt/AdGuardHome/AdGuardHome.yaml',
                     '/opt/AdGuardHome-unfiltered'}


def test_data_directories_are_recorded_without_content(root):
    (root / 'var/lib/caddy/certs/a.crt').write_text('x')
    found = files(root)
    assert found['/var/lib/caddy']['type'] == 'dir' and found['/var/lib/sing-box']['type'] == 'dir'
    assert '/var/lib/caddy/certs' not in found and '/var/lib/vmsingle' not in found
