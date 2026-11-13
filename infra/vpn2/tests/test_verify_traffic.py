"""Контрольный клиент verify (`vpn2.verify.traffic`): отчёт, пробы, ручные метки, отзыв IKEv2, очистка."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import types
import unittest
from unittest import mock

from vpn2.verify import traffic as vt


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


class CleanupLinkDownTests(unittest.TestCase):
    def cleanup_calls(self):
        calls = []

        def run(*args, **_):
            calls.append(('run',) + args)
            return subprocess.CompletedProcess(args, 0, '', '')

        def sleep(seconds):
            calls.append(('sleep', seconds))

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.multiple(vt, run=run, manual_markers=mock.Mock(),
                                     CCD=tmp / 'ccd', SWAN_FILE=tmp / 'swan.conf', WORK=tmp / 'work',
                                     MANUAL_DIR=tmp / 'manual'), \
                    mock.patch.object(vt.time, 'sleep', sleep):
                vt.cleanup()
        return calls

    def test_host_link_down_and_wait_before_removal(self):
        calls = self.cleanup_calls()
        removals = [i for i, call in enumerate(calls)
                    if call[:4] == ('run', 'ip', 'netns', 'del') or call == ('run', 'ip', 'link', 'del', vt.HOST_IF)]
        self.assertTrue(removals, calls)
        first = min(removals)
        downs = [i for i, call in enumerate(calls[:first])
                 if call == ('run', 'ip', 'link', 'set', vt.HOST_IF, 'down')
                 or call == ('run', 'ip', 'link', 'set', 'dev', vt.HOST_IF, 'down')]
        self.assertTrue(downs, f'{vt.HOST_IF} не опущен до удаления: {calls[:first + 1]}')
        waited = sum(call[1] for call in calls[downs[0]:first] if call[0] == 'sleep')
        self.assertGreaterEqual(waited, 0.5, f'нет паузы между down и удалением: {calls[downs[0]:first + 1]}')

    def test_removal_still_happens(self):
        calls = self.cleanup_calls()
        self.assertIn(('run', 'ip', 'netns', 'del', vt.NS), calls)
        self.assertIn(('run', 'ip', 'link', 'del', vt.HOST_IF), calls)


class FreeAddressesTests(unittest.TestCase):
    def test_latin1_comment_is_read_as_wg_reads_it(self):
        """Байт latin-1 после `#` в wg0.conf (wg его берёт) не роняет подбор адреса, LANG=C."""
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / 'wg0.conf'
            conf.write_bytes(b'[Interface]\nPrivateKey = KLKQERtKGmYrCPaUJZYYoFjRRNizdBvaUWfrjPG5I1Q= # caf\xe9\n\n'
                             b'[Peer]\nPublicKey = 0ivjKi39oR/BxsZp17GSMczPpE4CKHCaC1RbI3zedFg=\nAllowedIPs = 10.66.66.6/32\n')

            def run(*args, **_options):
                if args[0] == 'ip':
                    return types.SimpleNamespace(stdout=json.dumps([{'addr_info': [{'local': '10.66.66.1', 'prefixlen': 29}]}]))
                return types.SimpleNamespace(stdout='51820' if 'listen-port' in args else '')

            with mock.patch.multiple(vt, WG_CONF=conf, run=run):
                free = vt.WireGuard().free
        # /29: хосты .1-.6, .1 - сам wg0, .6 - пир wg0.conf (живой wg не показывает ни одного).
        self.assertEqual(free, ['10.66.66.5', '10.66.66.4'])


if __name__ == '__main__':
    unittest.main()
