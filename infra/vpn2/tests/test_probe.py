import io
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from vpn2 import probe
from vpn2.probe import ClashApi, probe_error

BASE = 'http://127.0.0.1:9090'


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Подмена opener: handler(req, timeout) -> bytes | исключение."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def open(self, req, timeout):
        self.calls.append((req, timeout))
        out = self.handler(req, timeout)
        if isinstance(out, BaseException):
            raise out
        return Resp(out if isinstance(out, bytes) else out.encode())


def const(value):
    def handler(*_args):
        return value
    return handler


def api(handler, secret='test-only'):
    op = FakeOpener(handler)
    return ClashApi(BASE, secret, opener=op), op


def test_probe_error_variants():
    err = urllib.error.HTTPError(BASE, 500, 'x', {}, None)
    assert probe_error(err) == 'код 500'
    assert probe_error(urllib.error.URLError(socket.timeout('t'))) == 'таймаут'
    assert probe_error(socket.timeout()) == 'таймаут'
    assert probe_error(urllib.error.URLError(ConnectionRefusedError())) == 'отказ соединения'
    assert probe_error(ConnectionRefusedError()) == 'отказ соединения'
    assert probe_error(ValueError('бум')) == 'ошибка: бум'


def test_delay_good():
    c, op = api(const('{"delay": 42}'))
    assert c.delay('warp', 'https://a.example', timeout_ms=5000, http_timeout=7) == (42, '')
    req, timeout = op.calls[0]
    assert timeout == 7
    assert req.full_url == f'{BASE}/proxies/warp/delay?url=https%3A%2F%2Fa.example&timeout=5000'
    assert req.get_header('Authorization') == 'Bearer test-only'


def test_delay_float_ok():
    c, _ = api(const('{"delay": 12.5}'))
    assert c.delay('t', 'u', timeout_ms=1, http_timeout=1) == (12.5, '')


@pytest.mark.parametrize('body', ['{}', '{"delay": 0}', '{"delay": true}', '{"delay": -3}',
                                  '{"delay": null}', '{"delay": "5"}', 'null', '[]'])
def test_delay_bad_bodies(body):
    c, _ = api(const(body))
    ms, reason = c.delay('t', 'u', timeout_ms=1, http_timeout=1)
    assert ms is None
    assert reason == f'clash-api без delay: {json.loads(body)!r}'


def test_delay_empty_body_and_broken_json():
    c, _ = api(const(b''))
    assert c.delay('t', 'u', timeout_ms=1, http_timeout=1) == (None, 'clash-api без delay: {}')
    c, _ = api(const('bad json'))
    ms, reason = c.delay('t', 'u', timeout_ms=1, http_timeout=1)
    assert ms is None and reason.startswith('ошибка: ')


def test_delay_never_raises():
    for exc in (urllib.error.HTTPError(BASE, 500, 'x', {}, None),
                urllib.error.URLError(socket.timeout()),
                ConnectionRefusedError(), OSError('нет сети'), RuntimeError('x')):
        c, _ = api(const(exc))
        ms, reason = c.delay('t', 'u', timeout_ms=1, http_timeout=1)
        assert ms is None and reason
    c, _ = api(const(urllib.error.HTTPError(BASE, 500, 'x', {}, None)))
    assert c.delay('t', 'u', timeout_ms=1, http_timeout=1)[1] == 'код 500'


def test_delay_quotes_tag_and_url():
    c, op = api(const('{"delay": 1}'))
    c.delay('hy2-узел direct/x', 'https://x.example/a b?q=ы', timeout_ms=1500, http_timeout=2)
    url = op.calls[0][0].full_url
    path, query = url[len(BASE):].split('?')
    assert path == '/proxies/hy2-%D1%83%D0%B7%D0%B5%D0%BB%20direct%2Fx/delay'
    assert urllib.parse.parse_qs(query) == {'url': ['https://x.example/a b?q=ы'],
                                            'timeout': ['1500']}


def test_alive_any_second_target():
    answers = iter([urllib.error.URLError('timeout'), '{"delay": 42}'])
    c, op = api(lambda *_args: next(answers))
    assert c.alive_any('warp', ['https://a.example', 'https://b.example'],
                       timeout_ms=5000, http_timeout=7) is True
    assert len(op.calls) == 2
    assert 'b.example' in op.calls[1][0].full_url
    assert all(t == 7 for _, t in op.calls)


def test_alive_any_all_bad_and_first_good():
    c, op = api(const('{"delay": 0}'))
    assert c.alive_any('w', ['u1', 'u2'], timeout_ms=1, http_timeout=1) is False
    assert len(op.calls) == 2
    c, op = api(const('{"delay": 9}'))
    assert c.alive_any('w', ['u1', 'u2'], timeout_ms=1, http_timeout=1) is True
    assert len(op.calls) == 1
    assert c.alive_any('w', [], timeout_ms=1, http_timeout=1) is False


def test_delay_many_mixed():
    def handler(req, _timeout):
        if 'dead' in req.full_url:
            return urllib.error.URLError(socket.timeout())
        return '{"delay": 100}'
    c, _ = api(handler)
    tags = ['hy2-a-direct', 'hy2-dead-warp', 'hy2-b-warp']
    delays, errors = c.delay_many(tags, 'u', timeout_ms=1500, http_timeout=2.0, deadline=2.5)
    assert delays == {'hy2-a-direct': 100, 'hy2-dead-warp': None, 'hy2-b-warp': 100}
    assert errors == {'hy2-dead-warp': 'таймаут'}


def test_delay_many_empty_and_repeat():
    c, _ = api(const('{"delay": 5}'))
    assert c.delay_many([], 'u', timeout_ms=1, http_timeout=1, deadline=1) == ({}, {})
    first = c.delay_many(['a'], 'u', timeout_ms=1, http_timeout=1, deadline=1)
    assert first == c.delay_many(['a'], 'u', timeout_ms=1, http_timeout=1, deadline=1)


def test_delay_many_deadline():
    gate = threading.Event()

    def handler(req, _timeout):
        if '/hang/' in req.full_url:
            gate.wait(10)
            return '{"delay": 1}'
        return '{"delay": 7}'
    c, _ = api(handler)
    t0 = time.monotonic()
    try:
        delays, errors = c.delay_many(['ok', 'hang'], 'u', timeout_ms=1, http_timeout=1,
                                      deadline=0.3)
        spent = time.monotonic() - t0
    finally:
        gate.set()
    assert delays == {'ok': 7, 'hang': None}
    assert errors == {}
    assert spent < 1.5


def test_request_empty_and_headers():
    c, op = api(const(b''))
    assert c.request('GET', '/x', timeout=3) == {}
    req, timeout = op.calls[0]
    assert timeout == 3
    assert req.get_header('Content-type') is None
    assert req.data is None
    c.request('PUT', '/x', {'name': 'узел'}, timeout=3)
    req = op.calls[1][0]
    assert req.get_header('Content-type') == 'application/json'
    assert json.loads(req.data) == {'name': 'узел'}
    assert req.get_method() == 'PUT'


def test_request_empty_secret_bearer():
    c, op = api(const('{}'), secret='')
    c.request('GET', '/x', timeout=1)
    assert op.calls[0][0].get_header('Authorization') == 'Bearer '


def test_request_errors_propagate():
    c, _ = api(const(urllib.error.URLError('x')))
    with pytest.raises(urllib.error.URLError):
        c.request('GET', '/x', timeout=1)
    c, _ = api(const('bad json'))
    with pytest.raises(ValueError):
        c.request('GET', '/x', timeout=1)


def test_proxies_select_proxy_connections_close():
    seen = []

    def handler(req, _timeout):
        seen.append((req.get_method(), req.full_url[len(BASE):], req.data))
        path = req.full_url[len(BASE):]
        if path == '/proxies':
            return '{"proxies": {"g": {"now": "hy2-a-direct"}}}'
        if path == '/connections':
            return '{"connections": [{"id": "c1"}]}'
        return b''
    c, _ = api(handler)
    assert c.proxies(timeout=5) == {'g': {'now': 'hy2-a-direct'}}
    assert c.connections(timeout=5) == [{'id': 'c1'}]
    c.select('grp узел', 'hy2-узел-direct', timeout=5)
    c.close('c1', timeout=5)
    assert c.proxy('youtube-auto', timeout=5) == {}
    assert seen[2][:2] == ('PUT', '/proxies/grp%20%D1%83%D0%B7%D0%B5%D0%BB')
    assert json.loads(seen[2][2]) == {'name': 'hy2-узел-direct'}
    assert seen[3][:2] == ('DELETE', '/connections/c1')
    assert seen[4][:2] == ('GET', '/proxies/youtube-auto')


def test_proxies_connections_empty():
    c, _ = api(const(b''))
    assert c.proxies(timeout=1) == {}
    assert c.connections(timeout=1) == []


CONFIG = {'experimental': {'clash_api': {'external_controller': '127.0.0.1:9090',
                                         'secret': 'test-only'}}}


def test_from_config_dict_and_file(tmp_path):
    c = ClashApi.from_config(CONFIG)
    assert (c.base, c.secret) == (BASE, 'test-only')
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(CONFIG), encoding='utf-8')
    for source in (path, str(path)):
        assert ClashApi.from_config(source).base == BASE


def test_from_config_missing():
    for config in ({}, {'experimental': {}}, {'experimental': None},
                   {'experimental': {'clash_api': {'secret': 's'}}}):
        with pytest.raises(KeyError):
            ClashApi.from_config(config)
        assert ClashApi.from_config(config, required=False) is None


def test_from_config_no_secret():
    config = {'experimental': {'clash_api': {'external_controller': '192.0.2.1:9090'}}}
    assert ClashApi.from_config(config).secret == ''


def test_from_config_local_only():
    for ctl in ('127.0.0.1:9090', 'localhost:9090', '[::1]:9090'):
        config = {'experimental': {'clash_api': {'external_controller': ctl}}}
        assert ClashApi.from_config(config, local_only=True).base == f'http://{ctl}'
    for ctl in ('10.99.0.1:9090', '127.0.0.1:9091', '0.0.0.0:9090'):
        config = {'experimental': {'clash_api': {'external_controller': ctl}}}
        with pytest.raises(ValueError):
            ClashApi.from_config(config, local_only=True)
    config = {'experimental': {'clash_api': {'external_controller': '198.51.100.5:9090'}}}
    assert ClashApi.from_config(config).base == 'http://198.51.100.5:9090'


def test_from_config_missing_file_and_broken(tmp_path):
    with pytest.raises(FileNotFoundError):
        ClashApi.from_config(tmp_path / 'nope.json')
    bad = tmp_path / 'bad.json'
    bad.write_text('{', encoding='utf-8')
    with pytest.raises(ValueError):
        ClashApi.from_config(bad)


def test_bad_base():
    for base in ('127.0.0.1:9090', 'http://h:1/', '', None):
        with pytest.raises(ValueError):
            ClashApi(base, 's')


def test_default_opener_is_shared():
    first = ClashApi(BASE, 's').opener
    for _ in range(100):
        assert ClashApi(BASE, 's').opener is first
    assert ClashApi.from_config(CONFIG).opener is first
    assert probe._default_opener() is first
    assert not any(type(h).__name__ == 'ProxyHandler' and h.proxies
                   for h in first.handlers)


def test_module_has_no_machine_paths():
    text = Path(probe.__file__).read_text(encoding='utf-8')
    assert '/home/' not in text
