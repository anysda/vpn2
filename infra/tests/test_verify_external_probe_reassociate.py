"""Повтор внешней пробы идёт через новую UDP-ассоциацию SOCKS5 ретранслятора.

tg-proxy подменён SOCKS5 на 127.0.0.1: первая UDP-ассоциация мёртвая (пакеты
принимает, не отвечает), следующие отвечают эхом. Ретранслятор настоящий.
Повтор через ту же мёртвую ассоциацию теряет все попытки, поэтому каждая
попытка должна открыть свою ассоциацию, а прежнюю закрыть - без утечки
сокетов и потоков. Все ассоциации мёртвые - по-прежнему одна ✗.
"""
import importlib.util
import io
import os
import select
import socket
import struct
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'infra/vpn2/src/vpn2/verify/traffic.py'


def load():
    spec = importlib.util.spec_from_file_location('verify_traffic', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vt = load()
CHECK = 'внешняя проба портов'
OPEN = 'udp/1194 открыт с адреса экзита, туннель передаёт данные'
CLOSED = 'udp/1194 не отвечает с адреса экзита (локально работает)'
WAN = '198.51.100.7'


def wait(condition, seconds=3.0):
    pause = threading.Event()
    for _ in range(int(seconds / 0.05)):
        if condition():
            return True
        pause.wait(0.05)
    return condition()


class Association:
    def __init__(self, control, live):
        self.control = control
        self.live = live
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(('127.0.0.1', 0))
        self.packets = 0
        self.closed = False


class FakeSocks:
    """SOCKS5 с UDP ASSOCIATE; живые ассоциации - начиная с номера `live_from`."""

    def __init__(self, live_from):
        self.live_from = live_from
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(8)
        self.associations = []
        self.lock = threading.Lock()
        self.stopping = False
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    @property
    def address(self):
        return self.server.getsockname()

    def handshake(self, control):
        control.settimeout(5)
        if control.recv(3) != b'\5\1\0':
            raise OSError('socks greeting')
        control.sendall(b'\5\0')
        request = control.recv(10)
        if len(request) < 10 or request[1] != 3:
            raise OSError('not udp associate')
        with self.lock:
            live = len(self.associations) + 1 >= self.live_from
            association = Association(control, live)
            self.associations.append(association)
        host, port = association.udp.getsockname()
        control.sendall(b'\5\0\0\1' + socket.inet_aton(host) + struct.pack('!H', port))
        control.settimeout(None)

    def loop(self):
        while not self.stopping:
            with self.lock:
                watched = [self.server] + [s for a in self.associations for s in (a.udp, a.control)
                                           if s is a.udp or not a.closed]
            ready = select.select(watched, [], [], 0.1)[0]
            for sock in ready:
                if sock is self.server:
                    control, _ = self.server.accept()
                    try:
                        self.handshake(control)
                    except OSError:
                        control.close()
                    continue
                with self.lock:
                    association = next(a for a in self.associations if sock in (a.udp, a.control))
                if sock is association.control:
                    try:
                        data = sock.recv(64)
                    except OSError:
                        data = b''
                    if not data:
                        association.closed = True
                    continue
                data, sender = sock.recvfrom(65535)
                association.packets += 1
                if association.live:
                    sock.sendto(data, sender)

    def fds(self):
        """Дескрипторы, которые держит сам подменённый tg-proxy."""
        with self.lock:
            return 1 + 2 * len(self.associations)

    def close(self):
        self.stopping = True
        self.thread.join(2)
        self.server.close()
        for association in self.associations:
            association.udp.close()
            association.control.close()


class FakeOpenVPN:
    name = 'OpenVPN'
    ports = [('udp', 1194)]

    def __init__(self):
        self.connects = 0

    def connect(self, _identity, endpoint):
        self.connects += 1
        assert endpoint[0] == '127.0.0.1', endpoint
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(1)
            client.sendto(b'openvpn-hello', endpoint)
            try:
                return client.recv(65535) == b'openvpn-hello'
            except OSError:
                return False


def open_fds():
    return len(os.listdir('/proc/self/fd'))


class ExternalProbeReassociateTests(unittest.TestCase):
    def run_external(self, live_from):
        proxy = FakeSocks(live_from)
        self.addCleanup(proxy.close)
        patches = [mock.patch.object(vt, 'TG_PROXY', proxy.address),
                   mock.patch.object(vt, 'HOST_IP', '127.0.0.1')]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        relay = vt.Relay(WAN)
        self.addCleanup(relay.stop)
        protocol = FakeOpenVPN()
        report = vt.Report('ru')
        verifier = vt.Verifier.__new__(vt.Verifier)
        verifier.report = report
        verifier.exits = ['us']
        verifier.relay = relay
        verifier.relay_ok = True
        verifier.echo_ok = lambda _protocol: True
        fds, threads = open_fds(), threading.active_count()
        with mock.patch.object(vt.time, 'sleep'), redirect_stdout(io.StringIO()):
            verifier.external(protocol)
        rows = [(row['result'], row['reason']) for row in report.rows if row['check'] == CHECK]
        return rows, report, protocol, proxy, fds, threads

    def test_retry_opens_new_association_and_closes_previous(self):
        rows, report, protocol, proxy, fds, threads = self.run_external(live_from=2)
        self.assertEqual(rows, [(vt.STATUSES[vt.OK], OPEN)])
        self.assertEqual(report.code(), 0)
        self.assertEqual(protocol.connects, 2)
        with proxy.lock:
            associations = list(proxy.associations)
        self.assertEqual(len(associations), protocol.connects,
                         'каждая попытка - своя UDP-ассоциация SOCKS5')
        self.assertTrue(all(a.packets >= 1 for a in associations),
                        [a.packets for a in associations])
        self.assertTrue(wait(lambda: all(a.closed for a in associations[:-1])),
                        'прежняя ассоциация не закрыта: управляющее TCP-соединение живо')
        # Ретранслятору остаётся только текущая сессия: listener, control, relay и один поток.
        self.assertTrue(wait(lambda: open_fds() - fds - proxy.fds() <= 3),
                        f'утечка сокетов: {open_fds() - fds - proxy.fds()}')
        self.assertTrue(wait(lambda: threading.active_count() - threads <= 1),
                        f'утечка потоков: {threading.active_count() - threads}')

    def test_first_attempt_success_is_single_association(self):
        rows, report, protocol, proxy, _, _ = self.run_external(live_from=1)
        self.assertEqual(rows, [(vt.STATUSES[vt.OK], OPEN)])
        self.assertEqual(report.code(), 0)
        self.assertEqual(protocol.connects, 1)
        self.assertEqual(len(proxy.associations), 1)

    def test_closed_port_still_fails_once(self):
        rows, report, protocol, proxy, fds, threads = self.run_external(live_from=10 ** 6)
        self.assertEqual(rows, [(vt.STATUSES[vt.FAIL], CLOSED)])
        self.assertEqual(report.code(), 1)
        self.assertEqual(protocol.connects, 3)
        self.assertTrue(wait(lambda: open_fds() - fds - proxy.fds() <= 3),
                        f'утечка сокетов: {open_fds() - fds - proxy.fds()}')
        self.assertTrue(wait(lambda: threading.active_count() - threads <= 1),
                        f'утечка потоков: {threading.active_count() - threads}')


if __name__ == '__main__':
    unittest.main()
