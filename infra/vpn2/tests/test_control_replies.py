"""Ответы vpn2-control против записанных примеров панели.

infra/tests/fixtures/control/panel-calls.json читают обе стороны: панель разбирает записанные
ответы (app/server/utils/control-contract.test.ts), здесь настоящий исполнитель отвечает на
записанные запросы. Форма ответа - имена полей на всю глубину и типы значений - обязана совпасть
с примером: поле, переименованное, добавленное, убранное или сменившее тип с одной стороны,
роняет тест.
"""
import base64
import json
import os
import re
import socket
from pathlib import Path

import pytest

from vpn2.control import backend as executor, contract, server

ROOT = Path(__file__).resolve().parents[3]
POOLS = json.loads((ROOT / 'app/shared/vpn-pools.json').read_text())
EXAMPLES = json.loads((ROOT / 'infra/tests/fixtures/control/panel-calls.json').read_text())
KEY_A = base64.b64encode(bytes(range(32))).decode()
KEY_B = base64.b64encode(bytes(range(1, 33))).decode()
SERIAL = '1234ABCD'
FAR = '2031-05-06T07:08:09.000Z'
DUE = '2026-11-01T00:00:00.000Z'
INDEX = '{status}\t300101000000Z\t\t' + SERIAL + '\tunknown\t/CN=dev_12\n'


def name(example):
    """Имя примера: метод и то, чем различаются запросы одного метода."""
    detail = example['params'].get('protocol') or example['params'].get('resource')
    return example['method'] + (':' + detail if detail else '')


def shape(value):
    """Значение без самих значений: имена полей, вложенность и типы."""
    if isinstance(value, dict):
        return {field: shape(item) for field, item in value.items()}
    if isinstance(value, list):
        return sorted({json.dumps(shape(item), sort_keys=True) for item in value})
    return type(value).__name__


def hollow(value, path='result'):
    """Пустые списки и объекты: за ними форма вложенных полей не видна."""
    if isinstance(value, dict):
        found = [where for field, item in value.items() for where in hollow(item, f'{path}.{field}')]
    elif isinstance(value, list):
        found = [where for item in value for where in hollow(item, path + '[]')]
    else:
        return []
    return found if value else [path]


class Host:
    """Хост службы в каталоге: файлы настоящие, wg, swanctl, openssl и management подставные."""

    def __init__(self, directory):
        self.root, self.state = directory / 'root', directory / 'state'
        self.state.mkdir()
        self.backend = executor.Backend(self.root, self.state, POOLS)
        self.backend.command, self.backend.management = self.command, self.management
        self.now = '2026-01-01T00:00:00.000Z'
        self.peers = {}     # ключ -> (PSK, адреса)
        self.sas = {}       # номер SA -> логин
        self.sessions = {}  # CN -> виртуальный адрес
        self.broken = ''
        self.file('/etc/wireguard/wg0.conf', '[Interface]\nPrivateKey = server-secret\nListenPort = 51820\n')
        self.file('/etc/swanctl/conf.d/anysda-clients.conf', '')
        self.file('/etc/openvpn/server/ccd/dev_12', 'ifconfig-push 10.67.67.2 255.255.255.0\n')
        self.file('/etc/openvpn/server/pki/index.txt', '')
        (self.root / 'etc/anysda').mkdir()

    def file(self, path, body):
        target = self.root / path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        return target

    def certificate(self, status='V'):
        self.file('/etc/openvpn/server/pki/index.txt', INDEX.format(status=status))
        self.file(f'/etc/openvpn/server/pki/newcerts/{SERIAL}.pem', 'test-cert')
        self.file('/etc/openvpn/server/pki/crl.pem', 'CRL')

    def ask(self, method, params):
        """Один запрос по сокету, как его шлёт панель."""
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with ours, theirs:
            ours.sendall(json.dumps({'api': 1, 'method': method, 'params': params}).encode() + b'\n')
            server.handle(theirs, self.backend, {os.getuid()}, POOLS)
            return json.loads(ours.makefile('rb').read())

    def done(self, method, params):
        reply = self.ask(method, params)
        assert reply['ok'], (method, reply)

    def command(self, *args, data=None):
        tool = args[0].rsplit('/', 1)[1]
        if tool == self.broken:
            raise contract.Fault('unavailable')
        return getattr(self, tool)(args, data) or ''

    def ip(self, _args, _data):
        return ''

    def wg(self, args, data):
        if args[1] == 'syncconf':
            self.peers = {}
            for block in data.split('[Peer]')[1:]:
                values = dict(line.split(' = ', 1) for line in block.strip().splitlines())
                self.peers[values['PublicKey']] = (values['PresharedKey'], values['AllowedIPs'].split(', '))
            return ''
        column = {'preshared-keys': lambda psk, _ips: psk, 'allowed-ips': lambda _psk, ips: ' '.join(ips),
                  'transfer': lambda _psk, _ips: '200\t100'}[args[-1]]
        return ''.join(f'{key}\t{column(*peer)}\n' for key, peer in self.peers.items())

    def swanctl(self, args, _data):
        if '--list-sas' in args:
            return ''.join(f"anysda-dev: #{number}, ESTABLISHED, IKEv2\n  remote EAP: '{login}'\n"
                           for number, login in self.sas.items())
        if '--list-conns' in args:
            loaded = (self.root / 'etc/swanctl/conf.d/anysda-clients.conf').read_text()
            return ''.join(f'{conn}: IKEv2, no reauthentication\n'
                           for conn in re.findall(r'(?m)^  (anysda-dev-\d+) : connections', loaded))
        if '--terminate' in args:
            self.sas.pop(int(args[-1]), None)
        return ''

    def openssl(self, args, _data):
        pki = self.root / 'etc/openvpn/server/pki'
        if '-out' in args:
            out = Path(args[args.index('-out') + 1])
            out.write_text({'genpkey': 'test-key-a', 'req': 'request'}.get(args[1], 'test-cert'))
        if args[1] == 'ca' and '-in' in args:
            self.certificate()
        elif '-revoke' in args:
            (pki / 'index.txt').write_text(INDEX.format(status='R'))
        elif '-pubout' in args or '-pubkey' in args:
            return 'public'
        elif '-serial' in args:
            return f'serial={SERIAL}\n'
        elif '-text' in args:
            return f'Revoked Certificates:\n    Serial Number: {SERIAL}\n'
        return ''

    def management(self, command):
        if command.startswith('kill '):
            self.sessions.pop(command[5:], None)
            return ['SUCCESS: killed']
        return [f'CLIENT_LIST,{cn},198.51.100.4:40000,{ip},,200,100,2026-01-01 00:00:00,1767225600,UNDEF,0,0'
                for cn, ip in self.sessions.items()] + ['END']


def profile_openvpn(host):
    for file in ('ca.crt', 'ca.key', 'openssl.cnf', 'tls-crypt.key'):
        host.file('/etc/openvpn/server/pki/' + file, 'test-' + file)


def profile_ikev2(host):
    for file, body in (('mode', 'letsencrypt'), ('server-host', '203.0.113.7'), ('domain', 'vpn.example.com')):
        host.file('/etc/anysda/ikev2-' + file, body + '\n')


def state_wg(host):
    host.done('wg.apply', {'version': 1, 'peers': [
        {'key': KEY_A, 'preshared_key': KEY_B, 'ip': '10.66.66.2', 'expires_at': FAR}]})


def state_openvpn(host):
    host.certificate()
    host.done('openvpn.apply', {'version': 1, 'device_id': 12, 'ip': '10.67.67.2', 'action': 'allow',
                                'expires_at': FAR})
    host.sessions['dev_12'] = '10.67.67.2'


def state_ikev2(host):
    host.done('ikev2.apply', {'version': 1, 'clients': [
        {'device_id': 12, 'username': 'user-12', 'password': 'test-pass-0012', 'ip': '10.68.68.2',
         'dns': ['10.99.0.1'], 'expires_at': FAR}]})
    host.sas[7] = 'user-12'


def traffic_ikev2(host):
    session = {'uniqueid': '7', 'username': 'user-12', 'rx': 100, 'tx': 200, 'ip': '10.68.68.2'}
    host.file('/etc/anysda/ikev2-status.json', json.dumps({'updated': executor.time.time(), 'sessions': [session]}))


def apply_ikev2(host):
    # Сессия, пережившая первую раскатку, в ответе повторной видна: это и есть поле sas.
    host.done('ikev2.apply', next(e for e in EXAMPLES if e['method'] == 'ikev2.apply')['params'])
    host.sas[42] = 'user-1'


def expiry(host):
    # OpenVPN снимается по сроку, WireGuard в это время недоступен: в ответе заняты все три списка.
    host.certificate()
    host.now = '2026-10-01T00:00:00.000Z'
    host.done('openvpn.apply', {'version': 1, 'device_id': 12, 'ip': '10.67.67.2', 'action': 'allow',
                                'expires_at': DUE})
    host.done('wg.apply', {'version': 1, 'peers': [
        {'key': KEY_A, 'preshared_key': KEY_B, 'ip': '10.66.66.2', 'expires_at': DUE}]})
    host.now, host.broken = '2026-11-01T00:00:05.000Z', 'wg'


def bare(_host):
    """Запросу хватает пустого хоста."""


# Что на хосте есть к моменту записанного запроса. Пример без строки здесь - красный тест.
PREPARE = {
    'profile.read:wg': lambda host: host.file('/etc/wireguard/server.pub', KEY_A + '\n'),
    'profile.read:openvpn': profile_openvpn,
    'profile.read:ikev2': profile_ikev2,
    'state.read:wg': state_wg,
    'state.read:openvpn': state_openvpn,
    'state.read:ikev2': state_ikev2,
    'traffic.read:wg': lambda host: host.peers.update({KEY_A: (KEY_B, ['10.66.66.2/32'])}),
    'traffic.read:openvpn': lambda host: host.sessions.update(dev_12='10.67.67.2'),
    'traffic.read:ikev2': traffic_ikev2,
    'wg.apply': bare,
    'ikev2.apply': apply_ikev2,
    'openvpn.apply': Host.certificate,
    'openvpn.issue': bare,
    'openvpn.revoke': Host.certificate,
    'openvpn.disconnect': lambda host: host.sessions.update(dev_12='10.67.67.2'),
    'openvpn.certificate': Host.certificate,
    'settings.read:disabled-exits': lambda host: host.file('/etc/anysda/exits-disabled.json', '{"disabled": ["fra"]}\n'),
    'settings.apply:disabled-exits': bare,
    'settings.apply:routes': bare,
    'settings.apply:preferences': bare,
    'routes.plan': lambda host: host.file('/etc/anysda/route-plan.json', '{"rules":[],"final":"direct-ru","rule_set":{}}'),
    'expiry.run': expiry,
}


@pytest.fixture
def host(tmp_path, monkeypatch):
    # OpenVPN читает CRL и ccd чужим пользователем: права файлов не должны зависеть от umask запуска.
    previous = os.umask(0o022)
    made = Host(tmp_path)
    monkeypatch.setattr(executor, 'utc_now', lambda: made.now)
    yield made
    os.umask(previous)


def test_every_recorded_reply_shows_its_nested_fields():
    assert {name(example): hollow(example['result']) for example in EXAMPLES if hollow(example['result'])} == {}


def test_recorded_requests_are_distinct():
    assert len({name(example) for example in EXAMPLES}) == len(EXAMPLES) == len(PREPARE)


@pytest.mark.parametrize('example', EXAMPLES, ids=name)
def test_service_reply_has_the_recorded_fields_and_types(example, host):
    PREPARE[name(example)](host)
    reply = host.ask(example['method'], example['params'])
    assert reply['ok'], reply
    assert shape(reply['result']) == shape(example['result'])
