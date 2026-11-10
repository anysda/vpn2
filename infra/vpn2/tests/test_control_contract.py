"""Контракт сокета vpn2-control: 17 методов, точные поля запроса, форма ответа и коды ошибок.

Панель (app/server/utils/control.ts) говорит с сервисом только этими запросами; перенос кода в пакет
не должен поменять ни имён, ни полей, ни конверта ответа.
"""
import base64
import json
import os
import socket
from pathlib import Path

import pytest

from vpn2.control import backend as executor, contract, server

POOLS = json.loads((Path(__file__).resolve().parents[3] / 'app/shared/vpn-pools.json').read_text())
KEY = base64.b64encode(bytes(range(1, 33))).decode()
PSK = base64.b64encode(bytes(range(33, 65))).decode()
WHEN = '2026-01-02T03:04:05.678Z'
PEER = {'key': KEY, 'preshared_key': PSK, 'ip': '10.66.66.2'}
CLIENT = {'device_id': 7, 'username': 'dev_7', 'password': 'Passw0rd-1234', 'ip': '10.68.68.2'}
METHODS = ('wg.apply', 'openvpn.apply', 'ikev2.apply', 'state.read', 'config.apply', 'check.run',
           'profile.read', 'traffic.read', 'openvpn.issue', 'openvpn.revoke', 'openvpn.disconnect',
           'openvpn.certificate', 'settings.read', 'settings.apply', 'api.describe', 'routes.plan', 'expiry.run')
# Метод -> параметры, которые сервис принимает (по одному полному примеру на метод).
VALID = {
    'wg.apply': {'version': 3, 'peers': [PEER, {**PEER, 'key': PSK, 'ip': '10.66.64.9', 'expires_at': WHEN}]},
    'openvpn.apply': {'version': 1, 'device_id': 7, 'action': 'allow', 'ip': '10.67.67.5', 'expires_at': None},
    'ikev2.apply': {'version': 2, 'clients': [{**CLIENT, 'dns': ['10.99.0.1'], 'expires_at': WHEN}]},
    'state.read': {'protocol': 'wg'},
    'config.apply': {'resource': 'wg-peers', 'schema': 1, 'version': 4, 'config': {'peers': [PEER]}},
    'check.run': {'check': 'readiness'},
    'profile.read': {'protocol': 'openvpn'},
    'traffic.read': {'protocol': 'ikev2'},
    'openvpn.issue': {'device_id': 7, 'generation': 'ab' * 32},
    'openvpn.revoke': {'device_id': 7, 'serial': '0A1b'},
    'openvpn.disconnect': {'device_id': 7},
    'openvpn.certificate': {'device_id': 7, 'serial': 'ff'},
    'settings.read': {'resource': 'routes'},
    'settings.apply': {'resource': 'routes', 'version': 5, 'config': [
        {'type': 'domain', 'value': '*.example.com', 'outbound': 'direct-ru'},
        {'type': 'ip_cidr', 'value': '192.0.2.0/24', 'outbound': 'hy2-nl-warp'}]},
    'api.describe': {},
    'routes.plan': {'offset': 0, 'revision': ''},
    'expiry.run': {},
}


def ask(method, params):
    return contract.validate({'api': 1, 'method': method, 'params': params}, POOLS)


def refused(method, params):
    with pytest.raises(contract.Fault) as caught:
        ask(method, params)
    return str(caught.value)


def test_method_names_and_limits_are_the_published_ones():
    assert contract.METHODS == METHODS and len(set(METHODS)) == 17
    assert (contract.API, contract.LIMIT, contract.MAX_DEVICES) == (1, 1024 * 1024, 2000)
    assert set(VALID) == set(METHODS)


@pytest.mark.parametrize('method', METHODS)
def test_each_method_takes_its_exact_params(method):
    params = VALID[method]
    if method == 'config.apply':
        assert ask(method, params) == ('wg.apply', {'version': 4, 'peers': [PEER]})
    else:
        assert ask(method, params) == (method, params)
    assert refused(method, {**params, 'extra': 1}) == 'validation'
    for name in params:
        rest = {k: v for k, v in params.items() if k != name}
        if (method, name) == ('openvpn.apply', 'expires_at'):
            assert ask(method, rest) == (method, rest)
        else:
            assert refused(method, rest) == 'validation', name


def test_config_apply_for_ikev2_is_the_protocol_method():
    params = {'resource': 'ikev2-clients', 'schema': 1, 'version': 9, 'config': {'clients': [CLIENT]}}
    assert ask('config.apply', params) == ('ikev2.apply', {'version': 9, 'clients': [CLIENT]})
    assert refused('config.apply', {**params, 'schema': 2}) == 'validation'
    assert refused('config.apply', {**params, 'config': {'peers': []}}) == 'validation'


def test_request_envelope_is_exact():
    good = {'api': 1, 'method': 'api.describe', 'params': {}}
    assert contract.validate(good, POOLS) == ('api.describe', {})
    for bad in ({**good, 'id': 1}, {**good, 'api': 2}, {**good, 'api': True}, {**good, 'api': 1.0},
                {**good, 'method': 'wg.read'}, {**good, 'method': None}, {'api': 1, 'method': 'api.describe'},
                [good], None):
        with pytest.raises(contract.Fault) as caught:
            contract.validate(bad, POOLS)
        assert str(caught.value) == 'validation'


@pytest.mark.parametrize(('method', 'change'), [
    ('wg.apply', {'version': 0}),
    ('wg.apply', {'version': 2**53}),
    ('wg.apply', {'version': '3'}),
    ('wg.apply', {'peers': [PEER, PEER]}),
    ('wg.apply', {'peers': [{**PEER, 'key': KEY[:-1] + '*'}]}),
    ('wg.apply', {'peers': [{**PEER, 'key': base64.b64encode(bytes(32)).decode()}]}),
    ('wg.apply', {'peers': [{**PEER, 'ip': '10.66.66.1'}]}),
    ('wg.apply', {'peers': [{**PEER, 'ip': '10.67.67.5'}]}),
    ('wg.apply', {'peers': [{**PEER, 'expires_at': '2026-01-02T03:04:05Z'}]}),
    ('wg.apply', {'peers': [PEER] * 2001}),
    ('openvpn.apply', {'action': 'restore'}),
    ('openvpn.apply', {'device_id': 0}),
    ('openvpn.apply', {'ip': '10.66.66.2'}),
    ('ikev2.apply', {'clients': [{**CLIENT, 'password': 'short'}]}),
    ('ikev2.apply', {'clients': [{**CLIENT, 'username': '-dash'}]}),
    ('ikev2.apply', {'clients': [{**CLIENT, 'dns': ['9.9.9.9']}]}),
    ('ikev2.apply', {'clients': [CLIENT, {**CLIENT, 'device_id': 8, 'ip': '10.68.68.3'}]}),
    ('state.read', {'protocol': 'ovpn'}),
    ('check.run', {'check': 'all'}),
    ('openvpn.issue', {'generation': 'A' * 64}),
    ('openvpn.revoke', {'serial': ''}),
    ('openvpn.certificate', {'serial': 'f' * 65}),
    ('settings.read', {'resource': 'exits'}),
    ('settings.apply', {'config': [{'type': 'domain', 'value': 'nodot', 'outbound': 'direct-ru'}]}),
    ('settings.apply', {'config': [{'type': 'ip_cidr', 'value': '192.0.2.0/33', 'outbound': 'direct-ru'}]}),
    ('settings.apply', {'config': [{'type': 'domain', 'value': 'a.example', 'outbound': 'hy2-NL-direct'}]}),
    ('routes.plan', {'offset': -1}),
    ('routes.plan', {'offset': 32 * 1024 * 1024 + 1}),
    ('routes.plan', {'revision': 'abc'}),
])
def test_bad_values_are_validation_faults(method, change):
    assert refused(method, {**VALID[method], **change}) == 'validation'


def test_settings_resources_have_their_own_schemas():
    assert ask('settings.apply', {'resource': 'disabled-exits', 'version': 1, 'config': {'disabled': ['nl', 'de-2']}})
    assert ask('settings.apply', {'resource': 'preferences', 'version': 1,
                                  'config': {'nl': ['10.66.66.2', '10.67.67.5', '10.68.68.9']}})
    assert refused('settings.apply', {'resource': 'disabled-exits', 'version': 1,
                                      'config': {'disabled': ['nl', 'nl']}}) == 'validation'
    assert refused('settings.apply', {'resource': 'preferences', 'version': 1,
                                      'config': {'nl': ['192.0.2.1']}}) == 'validation'
    assert refused('settings.apply', {'resource': 'preferences', 'version': 1, 'config': {'NL': []}}) == 'validation'


class Backend:
    """Двойник исполнителя: помнит вызовы, отвечает заданным или бросает заданное."""

    def __init__(self, answer=None):
        self.answer, self.calls = answer, []

    def execute(self, method, params):
        self.calls.append((method, params))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def exchange(raw, backend, uids=None):
    """Один запрос по сокету, как его шлёт панель: байты ответа до закрытия."""
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with ours, theirs:
        ours.sendall(raw)
        server.handle(theirs, backend, {os.getuid()} if uids is None else uids, POOLS)
        return ours.makefile('rb').read()


def line(method, params):
    return json.dumps({'api': 1, 'method': method, 'params': params}).encode() + b'\n'


def test_reply_is_one_json_line_with_result():
    backend = Backend({'ready': True, 'api': 1})
    raw = exchange(line('check.run', {'check': 'readiness'}), backend)
    assert raw.endswith(b'\n') and raw.count(b'\n') == 1
    assert json.loads(raw) == {'api': 1, 'ok': True, 'result': {'ready': True, 'api': 1}}
    assert backend.calls == [('check.run', {'check': 'readiness'})]


@pytest.mark.parametrize('code', ['validation', 'forbidden', 'stale', 'conflict', 'unavailable', 'limit'])
def test_fault_becomes_error_code_without_details(code):
    raw = exchange(line('state.read', {'protocol': 'wg'}), Backend(contract.Fault(code)))
    assert json.loads(raw) == {'api': 1, 'ok': False, 'error': code}


@pytest.mark.parametrize(('raw', 'answer', 'error'), [
    (b'not json\n', None, 'validation'),
    (b'{"api":1,"method":"api.describe","params":{},"params":{}}\n', None, 'validation'),
    (b'{"api":1,"method":"api.describe","params":{}}\n{}\n', None, 'validation'),
    (b'{"api":1,"method":"api.describe","params":{"secret":"s3"}}\n', None, 'validation'),
    (line('api.describe', {}), OSError('no such file: /etc/x'), 'unavailable'),
    (line('api.describe', {}), KeyError('peer'), 'unavailable'),
    (line('api.describe', {}), {'big': 'x' * (1024 * 1024)}, 'limit'),
])
def test_broken_requests_and_failures_keep_the_envelope(raw, answer, error):
    backend = Backend(answer)
    reply = exchange(raw, backend)
    assert json.loads(reply) == {'api': 1, 'ok': False, 'error': error}
    assert b's3' not in reply and b'/etc/x' not in reply
    assert bool(backend.calls) == (answer is not None)


def test_caller_outside_the_list_is_forbidden_and_not_executed(capsys):
    backend = Backend({})
    reply = exchange(line('api.describe', {}), backend, uids={os.getuid() + 1})
    assert json.loads(reply) == {'api': 1, 'ok': False, 'error': 'forbidden'}
    assert backend.calls == []
    assert capsys.readouterr().err == f'vpn2-control: forbidden caller uid={os.getuid()}\n'


def test_describe_lists_the_methods():
    real = executor.Backend.__new__(executor.Backend)
    assert real.execute('api.describe', {}) == {'api': 1, 'methods': list(METHODS)}
    assert real.execute('check.run', {'check': 'readiness'}) == {'ready': True, 'api': 1}
