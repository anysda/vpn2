"""Контрольный клиент 99-verify: отчёт, пробы, ручные метки и настоящий WireGuard в netns."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'infra/lib/verify-traffic.py'
TARGET = ROOT / 'infra/lib/verify-target.py'


def load():
    spec = importlib.util.spec_from_file_location('verify_traffic', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vt = load()


class ReportTests(unittest.TestCase):
    def test_failure_outranks_unmeasured(self):
        for statuses, code in (((vt.OK, vt.NOT_NEEDED), 0), ((vt.OK, vt.UNKNOWN), 2),
                               ((vt.UNKNOWN, vt.FAIL, vt.OK), 1), ((), 0)):
            with self.subTest(statuses=statuses), redirect_stdout(io.StringIO()):
                report = vt.Report('ru')
                for status in statuses:
                    report.add('проверка', 'WireGuard', status, 'причина')
                self.assertEqual(report.code(), code)
        report = vt.Report('ru')
        with redirect_stdout(io.StringIO()) as out:
            report.add('внешняя проба портов', 'IKEv2', vt.UNKNOWN, 'нет внешней точки')
            report.add('маршрут: прямой', 'OpenVPN', vt.FAIL, 'нет эха')
        # Формат строк, которые deploy.sh собирает в сводку предупреждений.
        self.assertEqual(out.getvalue().splitlines(), [
            '[ru] ? IKEv2: внешняя проба портов — не проверено: нет внешней точки',
            '[ru] ✗ OpenVPN: маршрут: прямой — нет эха'])
        self.assertEqual(set(report.rows[0]), {'check', 'component', 'result', 'reason', 'time'})
        self.assertEqual([r['result'] for r in report.rows], ['Не проверено', 'Ошибка'])


class ProbeTests(unittest.TestCase):
    def test_echo_requires_own_nonce(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        target = subprocess.Popen([sys.executable, '-I', str(TARGET), '--listen', '127.0.0.1',
                                   '--port', str(port), '--lifetime', '30'], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(target.stdout.readline().strip(), 'ready')
            self.assertEqual(vt.probe_echo('127.0.0.1', port, 'verify'), {'peer': '127.0.0.1', 'local': True})
            with socket.create_connection(('127.0.0.1', port)) as sock:
                sock.sendall(b'POST /vpn2-verify/' + b'a' * 32 + b' HTTP/1.0\r\nContent-Length: 32\r\n\r\n'
                             + b'b' * 32)
                self.assertIn(b' 400 ', sock.recv(4096))
        finally:
            target.terminate()
            target.wait()
        # Цель недоступна: проба не выдаёт успех.
        self.assertIsNone(vt.probe_echo('127.0.0.1', port, 'verify'))

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


class RelayHealthTests(unittest.TestCase):
    """Канал внешней пробы годен, только если экзит достаёт до entry снаружи."""

    def socks_stub(self, source):
        """SOCKS5 CONNECT, исходящий с адреса source - как tg-proxy на экзите."""
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()

        def serve():
            with server:
                conn, _ = server.accept()
                with conn:
                    conn.recv(3)
                    conn.sendall(b'\5\0')
                    request = conn.recv(10)
                    ip, port = socket.inet_ntoa(request[4:8]), int.from_bytes(request[8:10], 'big')
                    upstream = socket.socket()
                    upstream.bind((source, 0))
                    upstream.connect((ip, port))
                    conn.sendall(b'\5\0\0\1' + bytes(4) + b'\0\0')
                    with upstream:
                        upstream.sendall(conn.recv(4096))
                        while chunk := upstream.recv(4096):
                            conn.sendall(chunk)

        threading.Thread(target=serve, daemon=True).start()
        return server.getsockname()

    def test_health_needs_request_from_outside(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.2', 0))
            port = sock.getsockname()[1]
        target = subprocess.Popen([sys.executable, '-I', str(TARGET), '--listen', '127.0.0.2',
                                   '--port', str(port), '--lifetime', '30'], stdout=subprocess.PIPE, text=True)
        original = vt.TG_PROXY
        try:
            self.assertEqual(target.stdout.readline().strip(), 'ready')
            relay = vt.Relay('127.0.0.2')
            vt.TG_PROXY = self.socks_stub('127.0.0.1')
            self.assertTrue(relay.health(port), 'запрос пришёл с чужого адреса - канал есть')
            vt.TG_PROXY = self.socks_stub('127.0.0.2')
            self.assertFalse(relay.health(port), 'запрос с адреса самого entry - это не внешняя точка')
        finally:
            vt.TG_PROXY = original
            target.terminate()
            target.wait()
        # SOCKS не отвечает: внешней точки нет.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            vt.TG_PROXY = sock.getsockname()
        try:
            self.assertFalse(vt.Relay('127.0.0.2').health(port))
        finally:
            vt.TG_PROXY = original


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


class DirectRouteTests(unittest.TestCase):
    """Entry вне geoip-ru: его адрес sing-box отправляет в экзит, прямой выход - только по правилу."""

    def decide(self, host):
        # Порядок роутера: ручные наборы (direct-ru первым, как в 20-ru-router), гео-правила, final.
        for path in sorted(self.manual.glob('*.json'), key=lambda p: p.stem != 'direct-ru'):
            for rule in json.loads(path.read_text())['rules']:
                if any(host == d or host.endswith('.' + d.lstrip('.')) for d in rule.get('domain_suffix', [])):
                    return path.stem if path.stem == 'direct-ru' else path.stem[len('pin-'):], \
                        f'rule_set=manual-{path.stem}'
        # Адрес entry не в geoip-ru и не частный: foreign-best -> экзит.
        return 'hy2-se-direct', 'final'

    def test_direct_route_does_not_depend_on_entry_geoip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.manual = root / 'manual'
            self.manual.mkdir()
            for name in ('direct-ru', 'pin-hy2-se-direct'):
                (self.manual / f'{name}.json').write_text(json.dumps({'version': 2, 'rules': []}))
            router = root / 'config.json'
            router.write_text(json.dumps({'outbounds': [{'tag': 'direct-ru', 'type': 'direct'},
                                                        {'tag': 'hy2-se-direct', 'server': '203.0.113.9'}]}))
            saved = vt.MANUAL_DIR, vt.ROUTER, vt.INTERNET, vt.probe_dns
            vt.MANUAL_DIR, vt.ROUTER, vt.INTERNET, vt.probe_dns = self.manual, router, (), lambda *_: None
            try:
                report = vt.Report('ru')
                verifier = vt.Verifier(report, ['se'], '198.51.100.7', '198.51.100.7')
                verifier.targets.start = lambda **_: None
                verifier.internet_check = verifier.dns_check = lambda _protocol: None

                def probe(_protocol, _kind, args, *_, **__):
                    leaf, rule = self.decide(args[2])
                    return {'peer': '10.0.0.2', 'local': True}, [{'chains': [leaf], 'rule': rule, 'payload': ''}]

                verifier.probe = probe
                with redirect_stdout(io.StringIO()) as out:
                    verifier.prepare()
                    # Эхо на экзите (ssh) в тесте не поднимаем: считаем его запущенным.
                    verifier.targets.remote['se'] = None
                    verifier.paths(type('Protocol', (), {'name': 'IKEv2'})())
                print(out.getvalue().strip())
            finally:
                vt.MANUAL_DIR, vt.ROUTER, vt.INTERNET, vt.probe_dns = saved
            rows = {row['check']: row['result'] for row in report.rows}
            self.assertEqual(rows['маршрут: прямой'], 'Работает', out.getvalue())
            self.assertEqual(rows['маршрут: через экзит se'], 'Работает', out.getvalue())
            self.assertEqual(report.code(), 0)


# Настоящий сервер wg0 и клиент в netns vpn2-verify, внутри одноразовых сетевого
# и файлового пространств: на хост CI ничего не попадает.
NETNS_DRIVER = r'''
import importlib.util, json, subprocess, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location('vt', sys.argv[1])
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)
tmp = Path(sys.argv[2])
vt.WORK, vt.CCD, vt.SWAN_FILE, vt.MANUAL_DIR = tmp / 'work', tmp / 'ccd', tmp / 'swan.conf', tmp / 'manual'
run = vt.run
WAN = '198.51.100.1'
run('ip', 'link', 'set', 'lo', 'up')
run('ip', 'link', 'add', 'wan0', 'type', 'dummy')
run('ip', 'address', 'add', WAN + '/24', 'dev', 'wan0')
run('ip', 'link', 'set', 'wan0', 'up')
run('ip', 'route', 'add', 'default', 'dev', 'wan0')
run('ip', 'link', 'add', 'wg0', 'type', 'wireguard')
run('wg', 'set', 'wg0', 'private-key', '/dev/stdin', 'listen-port', '51820', data=run('wg', 'genkey').stdout)
run('ip', 'address', 'add', '10.66.66.1/24', 'dev', 'wg0')
run('ip', 'link', 'set', 'wg0', 'up')
result = {}
target = subprocess.Popen([sys.executable, '-I', sys.argv[3], '--listen', WAN, '--port', '18080',
                           '--lifetime', '120'], stdout=subprocess.PIPE, text=True)
assert target.stdout.readline().strip() == 'ready'


class Router:
    def connections(self):
        return []


def echo():
    return bool(vt.in_namespace(Router(), None, 'echo', [WAN, 18080, 'verify'], deadline=8)[0])


vt.cleanup()
vt.WORK.mkdir(mode=0o700)
vt.Namespace().up()
wg = vt.WireGuard()
addresses = wg.free_addresses(2)
result['addresses'] = addresses
wg.issue('a', addresses[0])
result['connect'] = wg.connect('a', (vt.HOST_IP, wg.port))
result['source'] = vt.Namespace.source()
result['echo'] = echo()
wg.down()
result['echo_tunnel_down'] = echo()
# Маршрут хоста есть, но клиенту разрешён только туннель: утечки мимо него нет.
vt.ns('ip', 'route', 'replace', 'default', 'via', vt.HOST_IP)
result['echo_host_route'] = echo()
wg.connect('a', (vt.HOST_IP, wg.port))
wg.revoke('a')
result['echo_revoked'] = echo()
wg.issue('b', addresses[1])
result['connect_b'] = wg.connect('b', (vt.HOST_IP, wg.port))
result['echo_b'] = echo()
# Обрыв посреди проверки: остатки снимает следующий запуск по признакам.
vt.cleanup()
result['leftovers'] = vt.leftovers()
result['peers'] = run('wg', 'show', 'wg0', 'peers').stdout.split()
result['input'] = run('iptables-save', '-t', 'filter').stdout.count('vpn2-verify')
target.terminate()
print(json.dumps(result))
'''


@unittest.skipUnless(os.environ.get('RUN_NETNS_TESTS') == '1' and shutil.which('wg'),
                     'set RUN_NETNS_TESTS=1 with sudo, iproute2, iptables and wireguard-tools')
class NetnsWireGuardTests(unittest.TestCase):
    def test_real_tunnel_positive_negative_revocation_cleanup(self):
        with tempfile.TemporaryDirectory(prefix='verify-traffic-') as directory:
            shell = ('mount -t tmpfs tmpfs /run && '
                     f'exec {sys.executable} -I -c "$0" "$@"')
            result = subprocess.run(['sudo', '-n', 'unshare', '--net', '--mount', 'sh', '-ec', shell,
                                     NETNS_DRIVER, str(MODULE), directory, str(TARGET)],
                                    text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr[-3000:])
            data = json.loads(result.stdout.splitlines()[-1])
            print(json.dumps(data, ensure_ascii=False))
            self.assertEqual(data['addresses'], ['10.66.66.254', '10.66.66.253'])
            self.assertTrue(data['connect'])
            self.assertEqual(data['source'], '10.66.66.254')
            self.assertTrue(data['echo'], 'положительный контроль: туннель передаёт данные')
            self.assertFalse(data['echo_tunnel_down'], 'отрицательный контроль: без туннеля нет данных')
            self.assertFalse(data['echo_host_route'], 'маршрут хоста не должен давать ответ')
            self.assertFalse(data['echo_revoked'])
            self.assertTrue(data['connect_b'])
            self.assertTrue(data['echo_b'])
            self.assertEqual(data['leftovers'], [])
            self.assertEqual(data['peers'], [])
            self.assertEqual(data['input'], 0)


if __name__ == '__main__':
    unittest.main()
