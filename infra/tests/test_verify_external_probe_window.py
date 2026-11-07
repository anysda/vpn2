"""Окно внешней пробы переживает минутный провал UDP через экзит и ограничено сверху.

На стенде все три попытки пробы udp/1194 через экзит потерялись за ~60 с, а
повтор через 2 мин прошёл: провал пути длиннее окна попыток. Часы виртуальные:
неудачный connect клиента OpenVPN стоит 20 с (его предел), пауза - сколько
просит проба. Ретранслятор настоящий, tg-proxy подменён SOCKS5 на 127.0.0.1:
UDP-ассоциация, открытая до конца провала, пакеты принимает и не отвечает.
"""
import importlib.util
import io
import select
import socket
import struct
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'infra/lib/verify-traffic.py'


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
CONNECT_LIMIT = 20.0
OUTAGE = 75.0
WINDOW_LIMIT = 150.0


class Clock:
    def __init__(self):
        self.now = 0.0
        self.lock = threading.Lock()

    def sleep(self, seconds):
        with self.lock:
            self.now += seconds


class FakeSocks:
    """SOCKS5 с UDP ASSOCIATE; ассоциация живая, если открыта после `outage_until`."""

    def __init__(self, clock, outage_until):
        self.clock = clock
        self.outage_until = outage_until
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
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(('127.0.0.1', 0))
        with self.lock:
            self.associations.append((control, udp, self.clock.now >= self.outage_until))
        control.sendall(b'\5\0\0\1' + socket.inet_aton('127.0.0.1') + struct.pack('!H', udp.getsockname()[1]))

    def loop(self):
        while not self.stopping:
            with self.lock:
                live = {udp: alive for _, udp, alive in self.associations}
            ready = select.select([self.server, *live], [], [], 0.1)[0]
            for sock in ready:
                if sock is self.server:
                    control, _ = self.server.accept()
                    try:
                        self.handshake(control)
                    except OSError:
                        control.close()
                    continue
                data, sender = sock.recvfrom(65535)
                if live[sock]:
                    sock.sendto(data, sender)

    def close(self):
        self.stopping = True
        self.thread.join(2)
        self.server.close()
        for control, udp, _ in self.associations:
            control.close()
            udp.close()


class FakeOpenVPN:
    """Клиент OpenVPN: ответ сервера - подключён, нет ответа - выходит по своему пределу."""

    name = 'OpenVPN'
    ports = [('udp', 1194)]

    def __init__(self, clock):
        self.clock = clock
        self.attempts = []

    def connect(self, _identity, endpoint):
        self.attempts.append(self.clock.now)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(1)
            client.sendto(b'openvpn-hello', endpoint)
            try:
                if client.recv(65535) == b'openvpn-hello':
                    return True
            except OSError:
                pass
        self.clock.sleep(CONNECT_LIMIT)
        return False


class ExternalProbeWindowTests(unittest.TestCase):
    def run_external(self, outage_until):
        clock = Clock()
        proxy = FakeSocks(clock, outage_until)
        self.addCleanup(proxy.close)
        for patch in (mock.patch.object(vt, 'TG_PROXY', proxy.address),
                      mock.patch.object(vt, 'HOST_IP', '127.0.0.1'),
                      mock.patch.object(vt.time, 'sleep', side_effect=clock.sleep)):
            patch.start()
            self.addCleanup(patch.stop)
        relay = vt.Relay(WAN)
        self.addCleanup(relay.stop)
        protocol = FakeOpenVPN(clock)
        report = vt.Report('ru')
        verifier = vt.Verifier.__new__(vt.Verifier)
        verifier.report = report
        verifier.exits = ['fi']
        verifier.relay = relay
        verifier.relay_ok = True
        verifier.echo_ok = lambda _protocol: True
        with redirect_stdout(io.StringIO()):
            verifier.external(protocol)
        rows = [(row['result'], row['reason']) for row in report.rows if row['check'] == CHECK]
        return rows, report, protocol, clock

    def test_minute_long_udp_outage_is_not_red(self):
        """Путь через экзит мёртв 75 с от первой попытки: проба дожидается его и зелёная."""
        rows, report, protocol, _ = self.run_external(OUTAGE)
        self.assertEqual(rows, [(vt.STATUSES[vt.OK], OPEN)], f'попытки на секундах {protocol.attempts}')
        self.assertEqual(report.code(), 0)

    def test_single_lost_answer_is_green_at_once(self):
        """Первая попытка без ответа, вторая с ответом - зелёная, лишних попыток нет."""
        rows, report, protocol, _ = self.run_external(1.0)
        self.assertEqual(rows, [(vt.STATUSES[vt.OK], OPEN)])
        self.assertEqual(report.code(), 0)
        self.assertEqual(len(protocol.attempts), 2)

    def test_dead_server_is_red_within_bounded_time(self):
        """Порт молчит всегда: одна ✗, три попытки, всё окно не дольше предела."""
        rows, report, protocol, clock = self.run_external(float('inf'))
        self.assertEqual(rows, [(vt.STATUSES[vt.FAIL], CLOSED)])
        self.assertEqual(report.code(), 1)
        self.assertEqual(len(protocol.attempts), 3)
        self.assertLessEqual(clock.now, WINDOW_LIMIT, f'попытки на секундах {protocol.attempts}')


if __name__ == '__main__':
    unittest.main()
