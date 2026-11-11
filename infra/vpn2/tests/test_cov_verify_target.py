"""Контрольная цель verify: отвечает только на свой nonce и называет, откуда пришёл запрос."""
import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from vpn2.verify import target

NONCE = '0123456789abcdef0123456789abcdef'


@pytest.fixture
def port():
    server = ThreadingHTTPServer(('127.0.0.1', 0), target.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(5)


def post(port, path, body, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        conn.request('POST', path, body=body, headers=headers or {})
        answer = conn.getresponse()
        return answer.status, answer.read()
    finally:
        conn.close()


def test_own_nonce_is_echoed_with_the_peer_address(port):
    started = time.monotonic()
    status, body = post(port, f'/vpn2-verify/{NONCE}', NONCE)
    assert status == 200
    assert json.loads(body) == {'nonce': NONCE, 'peer': '127.0.0.1', 'local': True}
    # Соединение держится: проверка успевает увидеть его в API sing-box.
    assert time.monotonic() - started >= 0.8


def test_repeat_gives_the_same_answer(port):
    first = post(port, f'/vpn2-verify/{NONCE}', NONCE)
    assert post(port, f'/vpn2-verify/{NONCE}', NONCE) == first


@pytest.mark.parametrize('path', [
    '/',
    '/vpn2-verify/',
    '/vpn2-verify/short',
    f'/vpn2-verify/{NONCE.upper()}',
    f'/vpn2-verify/{NONCE}0',
    f'/other/{NONCE}',
    f'/vpn2-verify/{NONCE}/extra',
])
def test_foreign_path_is_not_found(port, path):
    assert post(port, path, NONCE)[0] == 404


@pytest.mark.parametrize('body', ['', 'f' * 32, NONCE + 'tail', NONCE[:31]])
def test_body_must_repeat_the_nonce(port, body):
    assert post(port, f'/vpn2-verify/{NONCE}', body)[0] == 400


def test_get_is_refused(port):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    conn.request('GET', f'/vpn2-verify/{NONCE}')
    assert conn.getresponse().status == 501
    conn.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def start(*args):
    return subprocess.Popen([sys.executable, '-m', 'vpn2.verify.target', *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_command_says_ready_serves_and_leaves_by_itself():
    number = free_port()
    process = start('--listen', '127.0.0.1', '--port', str(number), '--lifetime', '3')
    try:
        assert process.stdout.readline() == 'ready\n'
        status, body = post(number, f'/vpn2-verify/{NONCE}', NONCE)
        assert status == 200 and json.loads(body)['local'] is True
        assert process.wait(15) == 0
    finally:
        process.kill()
        process.wait()
    with pytest.raises(OSError):
        post(number, f'/vpn2-verify/{NONCE}', NONCE)


def test_command_without_address_is_a_usage_error():
    process = start('--port', '1')
    _, err = process.communicate(timeout=15)
    assert process.returncode == 2
    assert '--listen' in err


def test_busy_port_is_an_error_before_ready():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        process = start('--listen', '127.0.0.1', '--port', str(sock.getsockname()[1]))
        out, err = process.communicate(timeout=15)
    assert process.returncode == 1
    assert out == '' and 'Address already in use' in err
