"""Единичная потеря внешней пробы udp/4500 не красит 99-verify.

Ретранслятор экзита подменён: первые N обменов через него теряются (ни
рукопожатие, ни эхо не доходят), дальше всё проходит. Одна потеря - проба
повторяется и даёт ✓; потеряны все попытки - ✗ с прежним текстом.
"""
import importlib.util
import io
from pathlib import Path
import unittest
from contextlib import redirect_stdout
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
OPEN = 'udp/4500 открыт с адреса экзита, туннель передаёт данные'
CLOSED = 'udp/4500 не отвечает с адреса экзита (локально работает)'


class LossyRelay:
    """Ретранслятор, у которого теряются первые `lost` обменов."""

    def __init__(self, lost):
        self.lost = lost
        self.exchanges = 0
        self.ports = {}

    def start(self, proto, port):
        relay_port = 40000 + len(self.ports)
        self.ports[relay_port] = (proto, port)
        return relay_port

    def exchange(self):
        self.exchanges += 1
        return self.exchanges > self.lost


class FakeIKEv2:
    name = 'IKEv2'
    ports = [('udp', 500), ('udp', 4500)]

    def __init__(self, relay, lose_in):
        self.relay = relay
        self.lose_in = lose_in
        self.tunnel = False
        self.connects = 0

    def connect(self, _identity, endpoint):
        self.connects += 1
        assert endpoint[0] == vt.HOST_IP and self.relay.ports[endpoint[1]] == ('udp', 4500), endpoint
        # Потеря в рукопожатии - клиент не подключился; потеря в эхе - подключился, данные не прошли.
        self.tunnel = self.relay.exchange() if self.lose_in == 'connect' else True
        return self.tunnel

    def echo(self):
        if not self.tunnel:
            return False
        return self.relay.exchange() if self.lose_in == 'echo' else True


def run_external(lost, lose_in):
    relay = LossyRelay(lost)
    protocol = FakeIKEv2(relay, lose_in)
    report = vt.Report('ru')
    verifier = vt.Verifier.__new__(vt.Verifier)
    verifier.report = report
    verifier.exits = ['us']
    verifier.relay = relay
    verifier.relay_ok = True
    verifier.markers = {vt.DIRECT: 'direct' + vt.MARKER}
    verifier.targets = mock.Mock(port=18080, echo=object())
    verifier.router = None

    def in_namespace(_router, _source, kind, _args, _dest):
        assert kind == 'echo', kind
        return protocol.echo(), None

    with mock.patch.object(vt, 'in_namespace', in_namespace), \
            mock.patch.object(vt.Namespace, 'source', staticmethod(lambda: '10.0.0.2')), \
            mock.patch.object(vt.time, 'sleep'), redirect_stdout(io.StringIO()):
        verifier.external(protocol)
    rows = [row for row in report.rows if row['check'] == CHECK]
    return rows, report, protocol


class ExternalProbeRetryTests(unittest.TestCase):
    def test_single_loss_is_retried(self):
        for lose_in in ('connect', 'echo'):
            with self.subTest(lose_in=lose_in):
                rows, report, protocol = run_external(1, lose_in)
                self.assertEqual([(row['result'], row['reason']) for row in rows],
                                 [(vt.STATUSES[vt.OK], OPEN)])
                self.assertEqual(report.code(), 0)
                self.assertGreaterEqual(protocol.connects, 2)

    def test_first_attempt_success_is_single(self):
        rows, report, protocol = run_external(0, 'connect')
        self.assertEqual([(row['result'], row['reason']) for row in rows], [(vt.STATUSES[vt.OK], OPEN)])
        self.assertEqual(report.code(), 0)
        self.assertEqual(protocol.connects, 1)

    def test_closed_port_still_fails(self):
        for lose_in in ('connect', 'echo'):
            with self.subTest(lose_in=lose_in):
                rows, report, _ = run_external(10 ** 6, lose_in)
                self.assertEqual([(row['result'], row['reason']) for row in rows],
                                 [(vt.STATUSES[vt.FAIL], CLOSED)])
                self.assertEqual(report.code(), 1)


if __name__ == '__main__':
    unittest.main()
