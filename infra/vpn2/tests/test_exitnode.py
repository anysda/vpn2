"""Шаги экзита: профиль WARP, секреты, сертификат, конфиг sing-box, смена портов."""
import base64
import json
import subprocess
import uuid
from pathlib import Path

import pytest

from vpn2 import exitnode

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / 'infra/configs/sing-box-server.json.tpl'
PROFILE = ('[Interface]\nPrivateKey = cHJpdg==\r\nAddress = 172.16.0.2/32, fd01::2/128\n'
           'DNS = 1.1.1.1\n[Peer]\nPublicKey = cHVi\nEndpoint = {endpoint}\n')
ACCOUNT = "access_token = 'tok-1'\ndevice_id = 'dev-1'\nlicense_key = 'x'\n"
ENV = {'PUB_IP': '203.0.113.7', 'HY2_DIRECT_PORT': '443', 'HY2_WARP_PORT': '444',
       'HY2_MGMT_PORT': '445'}


class Node:
    """Подмена wgcf, curl, sing-box и nft; openssl настоящий."""

    def __init__(self, etc):
        self.etc = etc
        self.calls = []
        self.said = []
        self.register = [0]
        self.generate = [(0, '')]
        self.endpoint = 'engage.cloudflareclient.com:2408'
        self.client_id = base64.b64encode(bytes([7, 8, 9])).decode()
        self.check = 0

    def say(self, text):
        self.said.append(text)

    def run(self, argv, **kwargs):
        name = Path(argv[0]).name
        if name == 'openssl':
            return subprocess.run(argv, **kwargs)
        self.calls.append([name, *argv[1:]])
        code, out = 0, ''
        if name == 'wgcf' and argv[1] == 'register':
            code = self.register.pop(0) if len(self.register) > 1 else self.register[0]
            out = 'Id: secret-device\nline2\nline3\nregister failed' if code else 'Id: secret-device'
            if code == 0:
                (self.etc / 'wgcf-account.toml').write_text(ACCOUNT)
        elif name == 'wgcf':
            code, out = self.generate.pop(0) if len(self.generate) > 1 else self.generate[0]
            if code == 0:
                (self.etc / 'wgcf-profile.conf').write_text(PROFILE.format(endpoint=self.endpoint))
        elif name == 'curl':
            assert kwargs['input'] == 'Authorization: Bearer tok-1\n'
            code = 0 if self.client_id else 22
            out = json.dumps({'config': {'client_id': self.client_id}}) if self.client_id else ''
        elif name == 'sing-box':
            code = self.check
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr='')


@pytest.fixture
def node(tmp_path):
    etc = tmp_path / 'etc'
    etc.mkdir()
    return Node(etc)


def test_fresh_node_registers_and_reads_profile(node):
    values = exitnode.warp_values(node.etc, node.say, node.run)
    assert values == {'WGCF_PRIVKEY': 'cHJpdg==', 'WGCF_LOCAL_IPV4': '172.16.0.2',
                      'WGCF_PEER_PUBKEY': 'cHVi',
                      'WGCF_PEER_ENDPOINT_HOST': 'engage.cloudflareclient.com',
                      'WGCF_PEER_ENDPOINT_PORT': '2408', 'WGCF_RESERVED_JSON': '[7,8,9]'}
    assert (node.etc / 'wgcf-client-id').read_text() == f'dev-1 {node.client_id}\n'
    for name in ('wgcf-account.toml', 'wgcf-profile.conf', 'wgcf-client-id'):
        assert (node.etc / name).stat().st_mode & 0o777 == 0o600
    # Id устройства из вывода register в лог не попадает.
    assert not any('secret-device' in line for line in node.said)


def test_known_client_id_is_not_requested_again(node):
    exitnode.warp_values(node.etc, node.say, node.run)
    node.calls.clear()
    node.client_id = ''
    values = exitnode.warp_values(node.etc, node.say, node.run)
    assert values['WGCF_RESERVED_JSON'] == '[7,8,9]'
    assert not [call for call in node.calls if call[0] == 'curl']


def test_unreachable_api_gives_zero_reserved_and_retries_next_run(node):
    node.client_id = ''
    assert exitnode.warp_values(node.etc, node.say, node.run)['WGCF_RESERVED_JSON'] == '[0,0,0]'
    assert not (node.etc / 'wgcf-client-id').exists()
    node.client_id = base64.b64encode(bytes([1, 2, 3, 4])).decode()
    assert exitnode.warp_values(node.etc, node.say, node.run)['WGCF_RESERVED_JSON'] == '[1,2,3]'


def test_failed_register_gives_placeholder_and_shows_only_tail(node):
    node.register = [1]
    assert exitnode.warp_values(node.etc, node.say, node.run) == exitnode.WARP_PLACEHOLDER
    assert 'register failed' in node.said
    assert not any('secret-device' in line for line in node.said)


def test_failed_generate_keeps_working_profile(node):
    exitnode.warp_values(node.etc, node.say, node.run)
    node.generate = [(1, 'cloudflare is down')]
    values = exitnode.warp_values(node.etc, node.say, node.run)
    assert values['WGCF_PRIVKEY'] == 'cHJpdg=='
    assert any('оставляю профиль прошлого прогона' in line for line in node.said)


def test_failed_generate_without_profile_gives_placeholder(node):
    (node.etc / 'wgcf-account.toml').write_text(ACCOUNT)
    node.generate = [(1, 'cloudflare is down')]
    assert exitnode.warp_values(node.etc, node.say, node.run) == exitnode.WARP_PLACEHOLDER


def test_legacy_account_returns_when_cloudflare_is_down(node):
    legacy = "device_id = 'dev-1'\naccess_token = 'tok-1'\n"
    (node.etc / 'wgcf-account.toml').write_text(legacy)
    (node.etc / 'wgcf-profile.conf').write_text(PROFILE.format(endpoint='162.159.192.9:2408'))
    node.generate = [(1, 'missing key_type in account')]
    node.register = [1]
    values = exitnode.warp_values(node.etc, node.say, node.run)
    assert values['WGCF_PEER_ENDPOINT_HOST'] == '162.159.192.9'
    assert (node.etc / 'wgcf-account.toml').read_text() == legacy
    assert not (node.etc / 'wgcf-account.toml.old').exists()


def test_legacy_account_without_profile_gives_placeholder(node):
    (node.etc / 'wgcf-account.toml').write_text("device_id = 'old'\n")
    node.generate = [(1, 'missing key_type in account')]
    node.register = [1]
    assert exitnode.warp_values(node.etc, node.say, node.run) == exitnode.WARP_PLACEHOLDER


def test_peer_address_keeps_ip_and_resolves_domain():
    said = []
    assert exitnode.peer_address('162.159.192.5', said.append) == '162.159.192.5'
    assert exitnode.peer_address('[2606:4700:d0::1]', said.append) == '2606:4700:d0::1'
    assert exitnode.peer_address('engage.example', said.append, lambda _host: ['198.51.100.4']) == '198.51.100.4'
    assert not said

    def offline(host):
        raise OSError(host)

    assert exitnode.peer_address('engage.example', said.append, offline) == exitnode.WARP_PEER_FALLBACK
    assert exitnode.peer_address('engage.example', said.append, lambda _host: []) == exitnode.WARP_PEER_FALLBACK
    assert len(said) == 2 and 'не разрешился' in said[0]


def test_secrets_are_created_once_and_kept(node):
    values, created = exitnode.ensure_secrets(node.etc, {}, node.run)
    assert created
    for name in ('hy2-direct.pwd', 'hy2-warp.pwd', 'hy2-obfs.pwd', 'hy2-mgmt.pwd', 'clash-secret.txt'):
        text = (node.etc / name).read_text()
        assert len(text) == 32 and '=' not in text and '\n' not in text
        assert (node.etc / name).stat().st_mode & 0o777 == 0o600
    assert len({values[name] for name in ('HY2_PWD_DIRECT', 'HY2_PWD_WARP', 'HY2_PWD_MGMT')}) == 3
    assert len(values['REALITY_SHORT_ID']) == 16
    assert len(base64.urlsafe_b64decode(values['REALITY_PRIVKEY'] + '=')) == 32
    public = dict(line.split('=', 1) for line in (node.etc / 'reality.pub').read_text().splitlines())
    assert public['SID'] == values['REALITY_SHORT_ID']
    assert (public['SNI'], public['PORT']) == ('www.apple.com', '4443')
    assert public['PUB'] != values['REALITY_PRIVKEY']
    again, created = exitnode.ensure_secrets(node.etc, {}, node.run)
    assert not created and again == values


def test_reality_settings_follow_config(node):
    values, _ = exitnode.ensure_secrets(node.etc, {'REALITY_SNI': 'example.org', 'REALITY_PORT': '8443'}, node.run)
    assert (values['REALITY_SNI'], values['REALITY_PORT']) == ('example.org', '8443')
    assert 'SNI=example.org\nPORT=8443\n' in (node.etc / 'reality.pub').read_text()


def test_cert_is_issued_once_and_reissued_for_new_address(node, tmp_path):
    box = tmp_path / 'box'
    box.mkdir()
    assert exitnode.ensure_cert(box, node.etc, '203.0.113.7', node.say, node.run)
    first = (box / 'tls.crt').read_bytes()
    assert (node.etc / 'hy2-tls.crt').read_bytes() == first
    assert (box / 'tls.key').stat().st_mode & 0o777 == 0o600
    assert not exitnode.ensure_cert(box, node.etc, '203.0.113.7', node.say, node.run)
    assert (box / 'tls.crt').read_bytes() == first
    # Адрес-префикс чужого SAN не считается совпадением.
    assert not exitnode.cert_ok(box / 'tls.crt', box / 'tls.key', '203.0.113.70', node.run)
    assert not exitnode.cert_ok(box / 'tls.crt', box / 'tls.key', '203.0.113', node.run)
    assert exitnode.ensure_cert(box, node.etc, '203.0.113.8', node.say, node.run)
    assert (node.etc / 'hy2-tls.crt').read_bytes() == (box / 'tls.crt').read_bytes() != first


def test_cert_with_foreign_key_is_reissued(node, tmp_path):
    box = tmp_path / 'box'
    box.mkdir()
    exitnode.ensure_cert(box, node.etc, '203.0.113.7', node.say, node.run)
    subprocess.run(['openssl', 'ecparam', '-name', 'prime256v1', '-genkey', '-noout',
                    '-out', str(box / 'tls.key')], check=True)
    assert exitnode.ensure_cert(box, node.etc, '203.0.113.7', node.say, node.run)


VALUES = {'HY2_DIRECT_PORT': '443', 'HY2_WARP_PORT': '444', 'HY2_MGMT_PORT': '445',
          'HY2_PWD_DIRECT': 'pd', 'HY2_PWD_WARP': 'pw', 'HY2_PWD_MGMT': 'pm', 'HY2_OBFS_PWD': 'po',
          'CLASH_SECRET': 'cs', 'HY2_BANDWIDTH_FIELDS': '', 'REALITY_PRIVKEY': 'rk',
          'REALITY_SHORT_ID': 'abcd', 'REALITY_SNI': 'www.apple.com', 'REALITY_PORT': '4443',
          **exitnode.WARP_PLACEHOLDER}


def test_config_has_reality_inbound_with_channel_routes():
    config = json.loads(exitnode.render_config(TEMPLATE.read_text(), VALUES))
    reality = config['inbounds'][-1]
    assert (reality['tag'], reality['type'], reality['listen_port']) == ('reality-in', 'vless', 4443)
    assert reality['users'] == [
        {'name': kind, 'uuid': str(uuid.uuid5(uuid.NAMESPACE_OID, 'vpn2-reality:' + password))}
        for kind, password in (('direct', 'pd'), ('warp', 'pw'), ('mgmt', 'pm'))]
    assert reality['tls']['reality']['short_id'] == ['abcd']
    rules = config['route']['rules']
    assert rules[-1] == {'inbound': 'reality-in', 'action': 'reject'}
    for kind in ('direct', 'warp', 'mgmt'):
        own = [rule for rule in rules if rule.get('auth_user') == [kind]]
        base = [rule for rule in rules if f'hy2-{kind}-in' in str(rule.get('inbound')) and 'auth_user' not in rule]
        assert own and len(own) == len(base)
        # Правило канала Reality стоит сразу за правилом своего hy2-инбаунда.
        assert rules.index(own[0]) == rules.index(base[0]) + 1
        assert {key: value for key, value in own[0].items() if key not in ('inbound', 'auth_user')} \
            == {key: value for key, value in base[0].items() if key != 'inbound'}
    assert '${' not in json.dumps(config)


def test_config_is_replaced_only_when_valid_and_different(node, tmp_path):
    box = tmp_path / 'box'
    box.mkdir()
    assert exitnode.apply_config(box, '{"a": 1}', node.run)
    assert not exitnode.apply_config(box, '{"a": 1}', node.run)
    assert (box / 'config.json').stat().st_mode & 0o777 == 0o600
    node.check = 1
    with pytest.raises(RuntimeError, match='невалиден'):
        exitnode.apply_config(box, '{"a": 2}', node.run)
    assert (box / 'config.json').read_text() == '{"a": 1}'
    assert [path.name for path in box.iterdir()] == ['config.json']


def test_configure_is_idempotent_and_applies_bandwidth(node, tmp_path):
    box = tmp_path / 'box'
    args = {'etc': node.etc, 'box': box, 'run': node.run}
    node.endpoint = '162.159.192.5:2408'
    assert exitnode.configure(ENV, TEMPLATE, node.say, **args)
    first = (box / 'config.json').read_text()
    assert '"up_mbps"' not in first and '162.159.192.5' in first
    assert not exitnode.configure(ENV, TEMPLATE, node.say, **args)
    assert exitnode.configure({**ENV, 'HY2_UP_MBPS': '50', 'HY2_DOWN_MBPS': '200'}, TEMPLATE, node.say, **args)
    config = json.loads((box / 'config.json').read_text())
    by_tag = {inbound['tag']: inbound for inbound in config['inbounds']}
    assert (by_tag['hy2-direct-in']['up_mbps'], by_tag['hy2-direct-in']['down_mbps']) == (50, 200)
    assert 'up_mbps' not in by_tag['hy2-mgmt-in']
    with pytest.raises(ValueError, match='PUB_IP'):
        exitnode.configure({**ENV, 'PUB_IP': ''}, TEMPLATE, node.say, **args)
