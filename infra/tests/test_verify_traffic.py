"""Контрольный клиент 99-verify: отчёт, пробы, ручные метки, отзыв IKEv2."""
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'infra/vpn2/src/vpn2/verify/traffic.py'


def load():
    spec = importlib.util.spec_from_file_location('verify_traffic', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vt = load()


class ReportTests(unittest.TestCase):
    def test_failure_outranks_unmeasured(self):
        for statuses, code in (((vt.OK, vt.OK), 0), ((vt.OK, vt.UNKNOWN), 2),
                               ((vt.UNKNOWN, vt.FAIL, vt.OK), 1), ((), 0)):
            with self.subTest(statuses=statuses), redirect_stdout(io.StringIO()):
                report = vt.Report('ru')
                for status in statuses:
                    report.add('проверка', 'WireGuard', status, 'причина')
                self.assertEqual(report.code(), code)
        report = vt.Report('ru')
        with redirect_stdout(io.StringIO()) as out:
            report.add('маршрут: прямой', 'IKEv2', vt.UNKNOWN, 'нет ручного набора')
            report.add('маршрут: прямой', 'OpenVPN', vt.FAIL, 'нет ответа')
        # Формат строк, которые deploy.sh собирает в сводку предупреждений.
        self.assertEqual(out.getvalue().splitlines(), [
            '[ru] ? IKEv2: маршрут: прямой — не проверено: нет ручного набора',
            '[ru] ✗ OpenVPN: маршрут: прямой — нет ответа'])
        self.assertEqual(set(report.rows[0]), {'check', 'component', 'result', 'reason', 'time'})
        self.assertEqual([r['result'] for r in report.rows], ['Не проверено', 'Ошибка'])


class ProbeTests(unittest.TestCase):
    def test_dns_answer_must_match_query(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(('127.0.0.1', 0))
            port = server.getsockname()[1]
            replies = []

            def serve():
                for mode in ('good', 'wrong-id', 'nxdomain'):
                    query, peer = server.recvfrom(512)
                    head = query[:2] if mode != 'wrong-id' else b'\0\0'
                    flags = b'\x81\x80' if mode != 'nxdomain' else b'\x81\x83'
                    answer = b'\xc0\x0c\0\1\0\1\0\0\0\x3c\0\4' + bytes([8, 8, 4, 4])
                    server.sendto(head + flags + b'\0\1\0\1\0\0\0\0' + query[12:] + answer, peer)
                    replies.append(mode)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            self.assertEqual(vt.probe_dns('127.0.0.1', 'dns.google', port), ['8.8.4.4'])
            self.assertIsNone(vt.probe_dns('127.0.0.1', 'dns.google', port))
            self.assertIsNone(vt.probe_dns('127.0.0.1', 'dns.google', port))
            thread.join(5)
            self.assertEqual(replies, ['good', 'wrong-id', 'nxdomain'])



class ManualMarkerTests(unittest.TestCase):
    def test_marker_added_and_removed_without_touching_owner_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            manual = Path(directory)
            owner = {'domain_suffix': ['example.org']}
            for name in ('pin-hy2-nl-direct', 'pin-hy2-nl-warp', 'direct-ru'):
                (manual / f'{name}.json').write_text(json.dumps({'version': 2, 'rules': [owner]}))
            original, vt.MANUAL_DIR = vt.MANUAL_DIR, manual
            try:
                markers = vt.manual_markers(['nl'], add=True)
                self.assertEqual(list(markers), ['nl'])
                self.assertTrue(markers['nl'].endswith('.nl.vpn2-verify.invalid'))
                pinned = json.loads((manual / 'pin-hy2-nl-direct.json').read_text())
                self.assertEqual(pinned['rules'], [{'domain_suffix': [markers['nl']]}, owner])
                self.assertEqual(oct((manual / 'pin-hy2-nl-direct.json').stat().st_mode & 0o777), '0o600')
                for name in ('pin-hy2-nl-warp', 'direct-ru'):
                    self.assertEqual(json.loads((manual / f'{name}.json').read_text())['rules'], [owner])
                # Повторный запуск после обрыва: старая метка снимается, новая одна.
                vt.manual_markers(['nl'], add=True)
                self.assertEqual(len(json.loads((manual / 'pin-hy2-nl-direct.json').read_text())['rules']), 2)
                vt.manual_markers((), add=False)
                for name in ('pin-hy2-nl-direct', 'pin-hy2-nl-warp', 'direct-ru'):
                    self.assertEqual(json.loads((manual / f'{name}.json').read_text())['rules'], [owner])
                self.assertEqual(sorted(p.name for p in manual.iterdir()),
                                 ['direct-ru.json', 'pin-hy2-nl-direct.json', 'pin-hy2-nl-warp.json'])
            finally:
                vt.MANUAL_DIR = original


class LingeringConnectionTests(unittest.TestCase):
    """Два экзита подряд к одному адресу: соединение прошлой пробы ещё в /connections."""

    def test_previous_probe_connection_is_not_counted(self):
        source, dest = '10.66.66.2', ('192.0.2.10', 80)
        listed = []

        class Process:
            def __init__(self, leaf):
                self.leaf, self.polls = leaf, 0

            def poll(self):
                self.polls += 1
                if self.polls == 2:
                    # Проба открыла своё соединение, пока процесс жив.
                    listed.append({'id': self.leaf, 'chains': [self.leaf], 'rule': 'rule_set', 'rulePayload': '',
                                   'metadata': {'sourceIP': source, 'destinationIP': dest[0],
                                                'destinationPort': '80'}})
                return None if self.polls < 4 else 0

            @staticmethod
            def kill():
                pass

            @staticmethod
            def communicate():
                return '404', None

        saved = vt.subprocess.Popen, vt.connections
        vt.connections = lambda: list(listed)
        try:
            leaves = []
            for tag in ('se', 'fi'):
                vt.subprocess.Popen = lambda *_a, _leaf=f'hy2-{tag}-direct', **_k: Process(_leaf)
                result, seen = vt.in_namespace(source, 'http', [dest[0], tag], dest, deadline=2)
                self.assertEqual(result, 404)
                leaves.append(sorted(leaf for leaf, _rule in seen))
        finally:
            vt.subprocess.Popen, vt.connections = saved
        # Соединение se закрыто клиентом, но sing-box ещё держит его в списке.
        self.assertEqual(len(listed), 2)
        self.assertEqual(leaves, [['hy2-se-direct'], ['hy2-fi-direct']])


class IKEv2RevokeTests(unittest.TestCase):
    """Отзыв снимает все IKE_SA логина, на какой бы conn charon их ни посадил."""

    def test_revoke_terminates_every_sa_of_identity(self):
        login = 'vpn2-verify-0a1b2c3d-a'
        # Две SA одного доступа: одна на общей conn, одна на проверочной; третья - чужая.
        sas = {'10': ('anysda-ikev2', login), '11': ('vpn2-verify-a', login), '12': ('anysda-dev-7', 'phone')}
        calls = []

        def fake_run(*args, **_kwargs):
            calls.append(args)
            out = ''
            if args[:2] == ('swanctl', '--list-sas'):
                # Как настоящий swanctl: EAP-логин виден, только если он не равен IKE-идентификатору.
                out = ''.join(f"{conn}: #{sa}, ESTABLISHED, IKEv2, 1_i 2_r*\n"
                              f"  remote '{user}' @ 203.0.113.5[4500] [10.68.68.2]\n"
                              f"  net: #{sa}, reqid 1, INSTALLED, TUNNEL, ESP:AES_GCM_16-256\n"
                              for sa, (conn, user) in sas.items())
            elif args[:3] == ('swanctl', '--terminate', '--ike-id'):
                sas.pop(args[3], None)
            elif args[:3] == ('swanctl', '--terminate', '--ike'):
                for sa in [sa for sa, (conn, _user) in sas.items() if conn == args[3]]:
                    sas.pop(sa)
            return subprocess.CompletedProcess(args, 0, out, '')

        ikev2 = vt.IKEv2.__new__(vt.IKEv2)
        ikev2.secrets = {'a': {'id': login, 'secret': 'x', 'revoked': False}}
        ikev2.server_conf = lambda: None
        saved = vt.run
        vt.run = fake_run
        try:
            ikev2.revoke('a')
        finally:
            vt.run = saved
        self.assertEqual(sorted(sas), ['12'], calls)



if __name__ == '__main__':
    unittest.main()
