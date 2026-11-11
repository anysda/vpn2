"""Служба vpn2-control на настоящем unix-сокете: один ответ на один запрос, отказ - словом."""
import json
import os
import socket
import threading

import pytest

from vpn2.control import server
from vpn2.control.contract import API, LIMIT, Fault

READY = {'api': API, 'method': 'check.run', 'params': {'check': 'readiness'}}


class Backend:
    """Сосед службы: помнит принятые запросы, может отказать."""

    def __init__(self):
        self.seen = []
        self.error = None

    def execute(self, method, params):
        self.seen.append((method, params))
        if self.error:
            raise self.error
        return {'done': len(self.seen)}


class Service:
    def __init__(self, path, allowed):
        self.path = str(path / 'control.sock')
        self.backend = Backend()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.path)
        self.listener.listen()
        self.thread = threading.Thread(target=self.loop, args=(allowed,), daemon=True)
        self.thread.start()

    def loop(self, allowed):
        try:
            server.serve(self.listener, self.backend, allowed, {})
        except OSError:
            pass

    def ask(self, payload, *, close=True):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode() + b'\n'
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(10)
            connection.connect(self.path)
            try:
                connection.sendall(payload)
                if close:
                    connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            data = connection.makefile('rb').read()
        assert data.endswith(b'\n') and data.count(b'\n') == 1
        return json.loads(data)

    def stop(self):
        self.listener.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        self.thread.join(5)


@pytest.fixture
def service(tmp_path):
    box = Service(tmp_path, {os.getuid()})
    yield box
    box.stop()


@pytest.fixture
def stranger(tmp_path):
    box = Service(tmp_path, {os.getuid() + 1})
    yield box
    box.stop()


def test_request_is_executed_and_answered(service):
    assert service.ask(READY) == {'api': API, 'ok': True, 'result': {'done': 1}}
    assert service.backend.seen == [('check.run', {'check': 'readiness'})]


def test_every_connection_gets_its_own_answer(service):
    answers = [service.ask(READY)['result']['done'] for _ in range(3)]
    assert answers == [1, 2, 3]


def test_answer_does_not_wait_for_the_caller_to_close(service):
    assert service.ask(READY, close=False)['ok'] is True


@pytest.mark.parametrize('payload', [
    b'\n',
    b'{broken\n',
    b'[]\n',
    b'"text"\n',
    b'{"api":1,"method":"check.run","params":{"check":"readiness"},"api":1}\n',
    b'{"api":1,"method":"no.such","params":{}}\n',
    b'{"api":2,"method":"check.run","params":{"check":"readiness"}}\n',
    b'{"api":1,"method":"check.run","params":{"check":"readiness"}}\ntail',
    b'[' * 5000 + b'\n',
])
def test_broken_request_is_refused_and_reaches_no_backend(service, payload):
    answer = service.ask(payload)
    assert answer == {'api': API, 'ok': False, 'error': 'validation'}
    assert service.backend.seen == []
    assert service.ask(READY)['ok'] is True


def test_silent_caller_is_refused(service):
    assert service.ask(b'') == {'api': API, 'ok': False, 'error': 'validation'}


def test_oversized_request_hits_the_limit(service):
    answer = service.ask(b'x' * (LIMIT + 1))
    assert answer == {'api': API, 'ok': False, 'error': 'limit'}
    assert service.backend.seen == []


@pytest.mark.parametrize('error', [OSError('disk'), KeyError('peer')])
def test_backend_trouble_is_unavailable_and_the_service_lives(service, error):
    service.backend.error = error
    assert service.ask(READY) == {'api': API, 'ok': False, 'error': 'unavailable'}
    service.backend.error = None
    assert service.ask(READY)['ok'] is True


def test_backend_refusal_is_passed_by_its_word(service):
    service.backend.error = Fault('conflict')
    assert service.ask(READY) == {'api': API, 'ok': False, 'error': 'conflict'}


def test_foreign_user_is_forbidden_and_named_in_the_journal(stranger, capfd):
    for _ in range(2):
        assert stranger.ask(READY) == {'api': API, 'ok': False, 'error': 'forbidden'}
    assert stranger.backend.seen == []
    assert capfd.readouterr().err.count(f'vpn2-control: forbidden caller uid={os.getuid()}\n') == 2


def test_start_outside_socket_activation_is_refused(monkeypatch):
    monkeypatch.delenv('LISTEN_PID', raising=False)
    monkeypatch.delenv('LISTEN_FDS', raising=False)
    with pytest.raises(Fault, match='unavailable'):
        server.main([])
    monkeypatch.setenv('LISTEN_PID', '1')
    monkeypatch.setenv('LISTEN_FDS', '1')
    with pytest.raises(Fault, match='unavailable'):
        server.main([])


@pytest.mark.skipif(os.path.exists('/run/vpn2-control/control.sock'), reason='на машине живая служба')
@pytest.mark.parametrize('flag', ['--probe', '--expire'])
def test_probe_without_the_service_fails(flag, capsys):
    with pytest.raises(OSError):
        server.main([flag])
    assert capsys.readouterr().out == ''
