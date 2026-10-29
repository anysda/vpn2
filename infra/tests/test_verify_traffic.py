"""Контрольный клиент 99-verify: отчёт, пробы, ручные метки и настоящий WireGuard в netns."""
import importlib.util
import io
import ipaddress
import json
import os
import re
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
                verifier.targets.start = lambda **_: setattr(verifier.targets, 'echo', True)
                verifier.internet_check = verifier.dns_check = lambda _protocol: None
                verifier.egress_check = lambda _protocol: None

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


def client_blocked(ip, port, rules, entry):
    """Отказ client_block_rules роутера для tcp-назначения (семантика sing-box).

    entry - адреса entry, которые vpn2-entry-addrs положил в набор entry-addrs.
    """
    address = ipaddress.ip_address(ip)
    # ip_is_private в sing-box - netip.IsPrivate и соседи, без документационных сетей.
    private = any(address in ipaddress.ip_network(net) for net in (
        '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', '169.254.0.0/16',
        '224.0.0.0/4', '0.0.0.0/32'))

    def match(rule):
        if rule.get('type') == 'logical':
            return all(match(r) for r in rule['rules'])
        if 'rule_set' in rule:
            return 'entry-addrs' in rule['rule_set'] and ip in entry
        if 'ip_cidr' in rule:
            return any(address in ipaddress.ip_network(net) for net in rule['ip_cidr'] if ':' not in net)
        if 'ip_is_private' in rule:
            return private
        if 'port' in rule:
            return (port in rule['port']) != bool(rule.get('invert'))
        return 'network' in rule and 'tcp' in rule['network']
    return any(match(rule) for rule in rules)


class EchoTargetTests(unittest.TestCase):
    """Эхо прямого пути, отзыва и отрицательного контроля - цель, которую роутер пускает клиенту."""

    def test_echo_target_is_open_to_client_and_leaves_direct(self):
        wan = '198.51.100.7'
        spec = importlib.util.spec_from_file_location('gen_router', ROOT / 'infra/lib/gen-router-config.py')
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        rules = gen.client_block_rules()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manual = root / 'manual'
            manual.mkdir()
            (manual / 'direct-ru.json').write_text(json.dumps({'version': 2, 'rules': []}))
            router = root / 'config.json'
            router.write_text(json.dumps({'outbounds': [{'tag': 'direct-ru', 'type': 'direct',
                                                         'bind_interface': 'eth0'}]}))
            saved = vt.MANUAL_DIR, vt.ROUTER, vt.INTERNET, vt.probe_dns
            vt.MANUAL_DIR, vt.ROUTER, vt.INTERNET, vt.probe_dns = manual, router, (), lambda *_: None
            sent = []
            state = {'up': True}
            try:
                report = vt.Report('ru')
                verifier = vt.Verifier(report, [], wan, wan)
                verifier.targets.start = lambda **_: setattr(verifier.targets, 'echo', True)
                verifier.internet_check = verifier.dns_check = verifier.egress_check = lambda _protocol: None

                def probe(_protocol, kind, args, *_, **__):
                    if kind != 'echo':
                        return None, []
                    ip, port, host = args
                    sent.append((ip, port))
                    # Отказ роутера или опущенный туннель - ответа нет; без метки direct-ru
                    # адрес вне geoip-ru ушёл бы в экзит.
                    if not state['up'] or client_blocked(ip, port, rules, [wan]):
                        return None, []
                    leaf = 'direct-ru' if host == verifier.markers.get('direct-ru') else 'foreign-best'
                    ok = leaf == 'direct-ru'
                    return ({'peer': ip, 'local': True} if ok else None), [{'chains': [leaf], 'rule': '',
                                                                          'payload': ''}]

                verifier.probe = probe
                protocol = type('Protocol', (), {'name': 'WireGuard',
                                                 'down': lambda _self: state.update(up=False)})()
                with redirect_stdout(io.StringIO()) as out:
                    verifier.prepare()
                    verifier.paths(protocol)
                    echo = verifier.echo_ok(protocol, retry=False)
                    verifier.negative(protocol)
            finally:
                vt.MANUAL_DIR, vt.ROUTER, vt.INTERNET, vt.probe_dns = saved
            print(out.getvalue().strip(), sent)
            rows = {row['check']: row['result'] for row in report.rows}
            self.assertEqual(rows['маршрут: прямой'], 'Работает', out.getvalue())
            self.assertTrue(echo, 'эхо отзыва и внешней пробы режет блок адресов entry')
            self.assertEqual(rows['отрицательный контроль'], 'Работает', out.getvalue())
            for ip, port in sent:
                self.assertFalse(client_blocked(ip, port, rules, [wan]), f'{ip}:{port} закрыт клиенту')
                self.assertNotEqual(ip, wan)


class ExitRouteTargetTests(unittest.TestCase):
    """Маршрут через экзит: цель, которую роутер пускает клиенту при любом адресе экзита."""

    def paths(self, server, closed=False):
        """Проверка пути через экзит se с адресом server; closed - исходящие sing-box на экзите запрещены."""
        wan = '198.51.100.7'
        spec = importlib.util.spec_from_file_location('gen_router', ROOT / 'infra/lib/gen-router-config.py')
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        rules = gen.client_block_rules()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manual = root / 'manual'
            manual.mkdir()
            for name in ('direct-ru', 'pin-hy2-se-direct'):
                (manual / f'{name}.json').write_text(json.dumps({'version': 2, 'rules': []}))
            router = root / 'config.json'
            router.write_text(json.dumps({'outbounds': [{'tag': 'direct-ru', 'type': 'direct'},
                                                        {'tag': 'hy2-se-direct', 'server': server}]}))
            saved = vt.MANUAL_DIR, vt.ROUTER, vt.INTERNET, vt.probe_dns
            vt.MANUAL_DIR, vt.ROUTER, vt.INTERNET, vt.probe_dns = manual, router, (), lambda *_: None
            sent = []
            try:
                report = vt.Report('ru')
                verifier = vt.Verifier(report, ['se'], wan, wan)
                verifier.targets.start = lambda **_: setattr(verifier.targets, 'echo', True)
                verifier.internet_check = verifier.dns_check = verifier.egress_check = lambda _protocol: None

                def probe(_protocol, kind, args, *_, **__):
                    ip, port, host = args
                    sent.append((ip, port))
                    # Отказ роутера entry (client_block_rules) - соединения и ответа нет.
                    if kind != 'echo' or client_blocked(ip, port, rules, [wan]):
                        return None, []
                    if host == verifier.markers.get('direct-ru'):
                        return ({'peer': ip, 'local': True} if ip == vt.ECHO_IP else None), \
                            [{'chains': ['direct-ru'], 'rule': 'rule_set=manual-direct-ru', 'payload': ''}]
                    seen = [{'chains': ['hy2-se-direct'], 'rule': 'rule_set=manual-pin-hy2-se-direct', 'payload': ''}]
                    # Эхо слушает на самом экзите: ответ есть, если sing-box экзита открыл соединение.
                    local = ip in (server, getattr(vt, 'EXIT_ECHO_IP', None))
                    return ({'peer': ip, 'local': True} if local and not closed else None), seen

                verifier.probe = probe
                with redirect_stdout(io.StringIO()) as out:
                    verifier.prepare()
                    verifier.targets.remote['se'] = None
                    verifier.paths(type('Protocol', (), {'name': 'WireGuard'})())
            finally:
                vt.MANUAL_DIR, vt.ROUTER, vt.INTERNET, vt.probe_dns = saved
        print(out.getvalue().strip())
        for ip, port in sent:
            self.assertFalse(client_blocked(ip, port, rules, [wan]), f'{ip}:{port} закрыт клиенту')
        return {row['check']: row['result'] for row in report.rows}

    def test_private_exit_route_is_checked_not_rejected(self):
        rows = self.paths('172.16.20.5')
        self.assertEqual(rows['маршрут: через экзит se'], 'Работает')
        self.assertEqual(rows['ручное правило se'], 'Работает')

    def test_exit_without_outbound_still_fails(self):
        for server in ('203.0.113.9', '172.16.20.5'):
            with self.subTest(server=server):
                self.assertEqual(self.paths(server)['маршрут: через экзит se'], 'Работает')
                rows = self.paths(server, closed=True)
                self.assertEqual(rows['маршрут: через экзит se'], 'Ошибка')
                self.assertEqual(rows['ручное правило se'], 'Ошибка')


class WgApplyTests(unittest.TestCase):
    """Пункт «WireGuard: применение пира»: тестовый пир через wg.apply службы управления."""

    KEYS = [__import__('base64').b64encode(bytes([n]) * 32).decode() for n in range(1, 8)]

    def serve(self, path, accept):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.listen()
        requests = []

        def loop():
            while True:
                try:
                    connection, _ = listener.accept()
                except OSError:
                    return
                with connection:
                    request = json.loads(connection.makefile('rb').readline())
                    requests.append(request)
                    if accept:
                        self.live = [peer['key'] for peer in request['params']['peers']]
                        reply = {'api': 1, 'ok': True, 'result': {'peers': {}}}
                    else:
                        reply = {'api': 1, 'ok': False, 'error': 'unavailable'}
                    connection.sendall(json.dumps(reply).encode() + b'\n')

        threading.Thread(target=loop, daemon=True).start()
        return listener, requests

    def fake_run(self, *args, data=None, **_options):
        out = ''
        if args[:4] == ('wg', 'show', 'wg0', 'peers'):
            out = '\n'.join(self.live)
        elif args[:4] == ('wg', 'show', 'wg0', 'allowed-ips'):
            out = ''
        elif args[:2] in (('wg', 'genkey'), ('wg', 'genpsk')):
            out = self.KEYS[6]
        elif args[:2] == ('wg', 'pubkey'):
            out = self.KEYS[5]
        elif args[:3] == ('wg', 'syncconf', 'wg0'):
            self.live = re.findall(r'PublicKey = (\S+)', data)
        elif args[:2] == ('ip', '-j'):
            out = json.dumps([{'addr_info': [{'local': '10.66.66.1', 'prefixlen': 24}]}])
        return subprocess.CompletedProcess(args, 0, out, '')

    def check(self, accept):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = root / 'wg0.conf'
            conf.write_text('[Interface]\nPrivateKey = x\nListenPort = 51820\n' + ''.join(
                f'\n[Peer]\nPublicKey = {key}\nPresharedKey = {self.KEYS[4]}\nAllowedIPs = 10.66.66.{n + 2}/32, '
                f'fd66:66::{n + 2}/128\n' for n, key in enumerate(self.KEYS[:2])))
            state = root / 'wg.json'
            state.write_text('{"version": 7, "digest": "d"}')
            before = conf.read_bytes(), state.read_bytes()
            self.live = self.KEYS[:2]
            listener, requests = self.serve(root / 'control.sock', accept)
            saved = vt.CONTROL_SOCKET, vt.CONTROL_WG_STATE, vt.WG_CONF, vt.WORK, vt.run
            vt.CONTROL_SOCKET, vt.CONTROL_WG_STATE, vt.WG_CONF = root / 'control.sock', state, conf
            vt.WORK, vt.run = root / 'work', self.fake_run
            vt.WORK.mkdir()
            try:
                report = vt.Report('ru')
                with redirect_stdout(io.StringIO()) as out:
                    vt.wg_apply_check(report)
            finally:
                listener.close()
                vt.CONTROL_SOCKET, vt.CONTROL_WG_STATE, vt.WG_CONF, vt.WORK, vt.run = saved
            print(out.getvalue().strip())
            self.assertEqual((conf.read_bytes(), state.read_bytes()), before, 'состояние до проверки не вернулось')
            self.assertEqual(self.live, self.KEYS[:2])
            return report, requests, out.getvalue()

    def test_refused_apply_is_failure(self):
        report, requests, out = self.check(accept=False)
        self.assertEqual([(r['component'], r['check'], r['result']) for r in report.rows],
                         [('WireGuard', 'применение пира', 'Ошибка')], out)
        self.assertEqual(report.code(), 1)
        self.assertEqual(requests[0]['method'], 'wg.apply')

    def test_applied_peer_seen_and_removed(self):
        report, requests, out = self.check(accept=True)
        self.assertEqual([r['result'] for r in report.rows], ['Работает'], out)
        self.assertEqual([r['params']['version'] for r in requests], [8, 9])
        self.assertEqual([len(r['params']['peers']) for r in requests], [3, 2])
        self.assertEqual(requests[0]['params']['peers'][2]['ip'], '10.66.66.254')


class ExitEgressTests(unittest.TestCase):
    """Туннель до экзита жив, а его выход наружу закрыт: эхо на экзите это не видит."""

    def test_closed_exit_egress_is_reported_per_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manual = root / 'manual'
            manual.mkdir()
            for name in ('direct-ru', 'pin-hy2-se-direct', 'pin-hy2-fi-direct'):
                (manual / f'{name}.json').write_text(json.dumps({'version': 2, 'rules': []}))
            router = root / 'config.json'
            router.write_text(json.dumps({'outbounds': [{'tag': 'direct-ru', 'type': 'direct'},
                                                        {'tag': 'hy2-se-direct', 'server': '203.0.113.9'},
                                                        {'tag': 'hy2-fi-direct', 'server': '203.0.113.10'}]}))
            closed = {'se'}

            def probe(_protocol, kind, args, *_, **__):
                if kind == 'internet':
                    # Без метки sing-box выбирает любой живой экзит - здесь fi.
                    return True, [{'chains': ['hy2-fi-direct'], 'rule': 'final', 'payload': ''}]
                host = args[2] if kind == 'echo' else args[1]
                tag = host.split('.')[1]
                leaf = 'direct-ru' if tag == 'direct-ru' else f'hy2-{tag}-direct'
                seen = [{'chains': [leaf], 'rule': f'rule_set=manual-pin-{leaf}', 'payload': ''}]
                if kind == 'echo':
                    # Эхо отвечает сам экзит: туннель до него жив и при закрытом выходе.
                    return {'peer': '10.0.0.2', 'local': True}, seen
                return (None if tag in closed else 404), seen

            names = 'MANUAL_DIR', 'ROUTER', 'INTERNET', 'probe_dns', 'resolve', 'probe_internet'
            saved = [getattr(vt, n) for n in names]
            for n, value in zip(names, (manual, router, (('check.example', '/generate_204', 204),),
                                        lambda *_: None, lambda _name: '192.0.2.10', lambda *_: True)):
                setattr(vt, n, value)
            try:
                report = vt.Report('ru')
                verifier = vt.Verifier(report, ['se', 'fi'], '198.51.100.7', '198.51.100.7')
                verifier.targets.start = lambda **_: None
                verifier.dns_check = lambda _protocol: None
                verifier.probe = probe
                with redirect_stdout(io.StringIO()) as out:
                    verifier.prepare()
                    verifier.targets.remote.update(se=None, fi=None)
                    verifier.targets.exit_ips.update(se='203.0.113.9', fi='203.0.113.10')
                    verifier.paths(type('Protocol', (), {'name': 'WireGuard'})())
            finally:
                for n, value in zip(names, saved):
                    setattr(vt, n, value)
            rows = {row['check']: row['result'] for row in report.rows}
            self.assertEqual(rows['маршрут: через экзит se'], 'Работает', out.getvalue())
            self.assertEqual(rows.get('интернет через экзит se'), 'Ошибка', out.getvalue())
            self.assertEqual(rows.get('интернет через экзит fi'), 'Работает', out.getvalue())
            self.assertEqual(report.code(), 1, out.getvalue())


class LingeringConnectionTests(unittest.TestCase):
    """Два экзита подряд к одному адресу: соединение прошлой пробы ещё в /connections."""

    def test_previous_probe_connection_is_not_counted(self):
        source, dest = '10.66.66.2', ('192.0.2.10', 80)
        listed = []

        class Router:
            @staticmethod
            def connections():
                return list(listed)

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

        saved = vt.subprocess.Popen
        try:
            leaves = []
            for tag in ('se', 'fi'):
                vt.subprocess.Popen = lambda *_a, _leaf=f'hy2-{tag}-direct', **_k: Process(_leaf)
                result, seen = vt.in_namespace(Router(), source, 'egress', [dest[0], tag], dest, deadline=2)
                self.assertEqual(result, 404)
                leaves.append(sorted(s['chains'][0] for s in seen))
        finally:
            vt.subprocess.Popen = saved
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


# Настоящий сервер wg0 и клиент в netns vpn2-verify, внутри одноразовых сетевого
# и файлового пространств: на хост CI ничего не попадает. У каждого сетевого шага
# свой срок: зависание называет шаг, а не общий таймаут теста.
NETNS_DRIVER = r'''
import contextlib, importlib.util, json, signal, socket, subprocess, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location('vt', sys.argv[1])
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)
# Рабочие файлы - в tmpfs /run этого пространства имён: исчезают вместе с ним.
tmp = Path('/run/vpn2-verify-test')
tmp.mkdir()
vt.WORK, vt.CCD, vt.SWAN_FILE, vt.MANUAL_DIR = tmp / 'work', tmp / 'ccd', tmp / 'swan.conf', tmp / 'manual'
run = vt.run
WAN = '198.51.100.1'
# Общий срок драйвера внутри таймаута теста (240 с). Готовность дочернего процесса ждём
# в его пределах, а не коротким сроком шага: старт интерпретатора тормозит загрузка раннера.
DEADLINE = time.monotonic() + 200


def left():
    return max(1, int(DEADLINE - time.monotonic()))


class StepTimeout(Exception):
    pass


@contextlib.contextmanager
def step(name, seconds):
    started = time.monotonic()

    def expired(*_args):
        raise StepTimeout(f'шаг «{name}» не уложился в {seconds} с')

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    done = False
    try:
        yield
        done = True
    finally:
        signal.alarm(0)
        print(f'шаг «{name}»: {"" if done else "сбой, "}{time.monotonic() - started:.2f} с',
              file=sys.stderr, flush=True)


class Router:
    def connections(self):
        return []


def started(process, err):
    # Живая, но молчащая цель упирается в срок шага; вышедшая - сразу отказ с кодом.
    if not vt.Targets.wait_ready(process, timeout=left() + 5):
        raise AssertionError(f'цель вышла без «ready»: код {process.wait(5)}, stderr: {err.read_text()[-500:]!r}')


def echo(name):
    # Проба сама сдаётся через 5 с соединения; запас срока - на старт её интерпретатора.
    with step(name, 35):
        return bool(vt.in_namespace(Router(), None, 'echo', [WAN, 18080, 'verify'], deadline=30)[0])


result, target, targets, exit_target = {}, None, None, None
try:
    with step('wan0 и сервер wg0', 15):
        run('ip', 'link', 'set', 'lo', 'up')
        run('ip', 'link', 'add', 'wan0', 'type', 'dummy')
        run('ip', 'address', 'add', WAN + '/24', 'dev', 'wan0')
        run('ip', 'link', 'set', 'wan0', 'up')
        run('ip', 'route', 'add', 'default', 'dev', 'wan0')
        run('ip', 'link', 'add', 'wg0', 'type', 'wireguard')
        run('wg', 'set', 'wg0', 'private-key', '/dev/stdin', 'listen-port', '51820', data=run('wg', 'genkey').stdout)
        run('ip', 'address', 'add', '10.66.66.1/24', 'dev', 'wg0')
        run('ip', 'link', 'set', 'wg0', 'up')
    with step('цель на WAN', left()):
        # stderr - в файл, не в канал теста: живая цель не держит его после падения драйвера.
        target = subprocess.Popen([sys.executable, '-I', sys.argv[2], '--listen', WAN, '--port', '18080',
                                   '--lifetime', '120'], stdout=subprocess.PIPE,
                                  stderr=(tmp / 'target.err').open('w'), text=True)
        started(target, tmp / 'target.err')
    with step('netns клиента и туннель a', 25):
        vt.cleanup()
        vt.WORK.mkdir(mode=0o700)
        vt.Namespace().up()
        wg = vt.WireGuard()
        addresses = wg.free_addresses(2)
        result['addresses'] = addresses
        wg.issue('a', addresses[0])
        result['connect'] = wg.connect('a', (vt.HOST_IP, wg.port))
        result['source'] = vt.Namespace.source()
    result['echo'] = echo('эхо через туннель a')
    wg.down()
    result['echo_tunnel_down'] = echo('эхо без туннеля')
    # Маршрут хоста есть, но клиенту разрешён только туннель: утечки мимо него нет.
    vt.ns('ip', 'route', 'replace', 'default', 'via', vt.HOST_IP)
    result['echo_host_route'] = echo('эхо по маршруту хоста')
    with step('отзыв a', 15):
        wg.connect('a', (vt.HOST_IP, wg.port))
        wg.revoke('a')
    result['echo_revoked'] = echo('эхо после отзыва')
    with step('туннель b', 15):
        wg.issue('b', addresses[1])
        result['connect_b'] = wg.connect('b', (vt.HOST_IP, wg.port))
    result['echo_b'] = echo('эхо через туннель b')
    # Эхо прямого пути: локальный маршрут через WAN выхода direct-ru, ответ сокету, привязанному к WAN.
    # Адреса WAN не меняются: новый адрес sing-box принимает за смену сети и рвёт соединения hy2.
    wan_addresses = run('ip', '-o', 'address', 'show', 'dev', 'wan0').stdout
    with step('эхо прямого пути: цели', 45):
        targets = vt.Targets(WAN, [], type('R', (), {'outbounds': {'direct-ru': {'bind_interface': 'wan0'}}})())
        targets.start(lifetime=60)
    result['wan_addresses_kept'] = run('ip', '-o', 'address', 'show', 'dev', 'wan0').stdout == wan_addresses
    with step('эхо прямого пути: ответ', 10):
        bound = socket.socket()
        bound.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'wan0')
        bound.settimeout(5)
        bound.connect((vt.ECHO_IP, targets.port))
        bound.sendall(f'POST /vpn2-verify/{"0" * 32} HTTP/1.0\r\nContent-Length: 32\r\n\r\n{"0" * 32}'.encode())
        # Заголовки и тело цель пишет разными send: ответ HTTP/1.0 читаем до закрытия соединения.
        reply = b''.join(iter(lambda: bound.recv(4096), b'')).decode()
        bound.close()
        result['echo_direct'] = json.loads(reply.split('\r\n\r\n', 1)[1])['local']
        result['echo_global'] = vt.ECHO_IP in run('ip', '-o', 'address', 'show', 'scope', 'global').stdout
    with step('эхо прямого пути: остановка', 15):
        targets.stop()
        result['echo_leftover'] = 'адрес эха' in vt.leftovers()
    # Цель на экзите - та же команда, что уходит по ssh: локальный маршрут на lo, эхо на всех адресах.
    with step('эхо экзита: цель', left()):
        exit_target = subprocess.Popen(['bash', '-c', vt.Targets.exit_command(18081, 60)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=(tmp / 'exit.err').open('w'), text=True)
        exit_target.stdin.write(Path(sys.argv[2]).read_text())
        exit_target.stdin.close()
        started(exit_target, tmp / 'exit.err')
    with step('эхо экзита', 20):
        with socket.create_connection((vt.EXIT_ECHO_IP, 18081), timeout=5) as sock:
            sock.sendall(f'POST /vpn2-verify/{"1" * 32} HTTP/1.0\r\nContent-Length: 32\r\n\r\n{"1" * 32}'.encode())
            reply = b''.join(iter(lambda: sock.recv(4096), b'')).decode()
        result['exit_echo'] = json.loads(reply.split('\r\n\r\n', 1)[1])['local']
        result['exit_echo_global'] = vt.EXIT_ECHO_IP in run('ip', '-o', 'address', 'show').stdout
        run('bash', '-c', vt.Targets.exit_stop_command())
        exit_target.wait(5)
        result['exit_echo_route_left'] = vt.EXIT_ECHO_IP in run('ip', 'route', 'show', 'table', 'local').stdout
    # Обрыв посреди проверки: остатки снимает следующий запуск по признакам.
    with step('уборка по признакам', 30):
        vt.cleanup()
        result['leftovers'] = vt.leftovers()
        result['peers'] = run('wg', 'show', 'wg0', 'peers').stdout.split()
        result['input'] = run('iptables-save', '-t', 'filter').stdout.count('vpn2-verify')
finally:
    for process in [target, exit_target] + ([targets.local, targets.echo] if targets else []):
        if process and process.poll() is None:
            process.kill()
print(json.dumps(result))
'''


@unittest.skipUnless(os.environ.get('RUN_NETNS_TESTS') == '1' and shutil.which('wg'),
                     'set RUN_NETNS_TESTS=1 with sudo, iproute2, iptables and wireguard-tools')
class NetnsWireGuardTests(unittest.TestCase):
    def test_real_tunnel_positive_negative_revocation_cleanup(self):
        shell = ('mount -t tmpfs tmpfs /run && '
                 f'exec {sys.executable} -I -c "$0" "$@"')
        try:
            result = subprocess.run(['sudo', '-n', 'unshare', '--net', '--mount', 'sh', '-ec', shell,
                                     NETNS_DRIVER, str(MODULE), str(TARGET)],
                                    text=True, capture_output=True, timeout=240)
        except subprocess.TimeoutExpired as error:
            stderr = error.stderr.decode(errors='replace') if isinstance(error.stderr, bytes) else error.stderr
            self.fail(f'драйвер netns не завершился за {error.timeout} с:\n{(stderr or "")[-3000:]}')
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
        self.assertTrue(data['echo_direct'], 'эхо прямого пути отвечает сокету direct-ru со своего адреса')
        self.assertFalse(data['echo_global'], 'адрес эха не должен попасть в набор entry-addrs')
        self.assertTrue(data['wan_addresses_kept'], 'эхо прямого пути не должно менять адреса WAN')
        self.assertTrue(data['echo_leftover'])
        self.assertTrue(data['exit_echo'], 'эхо экзита отвечает sing-box экзита с его собственного адреса')
        self.assertFalse(data['exit_echo_global'], 'адрес эха экзита не должен появиться на интерфейсе')
        self.assertFalse(data['exit_echo_route_left'], 'остановка цели снимает локальный маршрут на экзите')
        self.assertEqual(data['leftovers'], [])
        self.assertEqual(data['peers'], [])
        self.assertEqual(data['input'], 0)


if __name__ == '__main__':
    unittest.main()
