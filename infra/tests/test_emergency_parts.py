"""Части аварийного режима на их границах: снимок и его рассылка, очередь
Telegram, тексты сообщений, выдача Hysteria2."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'infra/vpn2/src'))
from vpn2.emergency import hy2, notices, snapshot, snapshot_sender, spool  # noqa: E402
from vpn2.notify import Reply  # noqa: E402

NOW = 1_800_000_000


def image(**changes):
    value = dict(schema=1, version=1, installation_id='inst', created_at=snapshot.iso_time(NOW),
                 admin_chat_id=900, bot_token='1:token',
                 exits=[dict(id='exit1', name='Амстердам', ip='192.0.2.10', priority=0)],
                 clients=[dict(id=1, name='Аня', telegram_chat_id=101, allowed=True, expires_at=None,
                               devices=[dict(id=11, name='телефон')])])
    return value | changes


class Temp(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())


class Snapshot(Temp):
    def test_expiry_applies_at_read_time_and_bad_files_are_no_snapshot(self):
        value = image()
        value['clients'][0]['expires_at'] = snapshot.iso_time(NOW + 60)
        value['clients'].append(dict(id=2, name='Боря', telegram_chat_id=None, allowed='yes',
                                     expires_at='когда-нибудь', devices=[]))
        (self.directory / snapshot.NAME).write_bytes(snapshot.encode(value))
        self.assertEqual([c['allowed'] for c in snapshot.load(self.directory, NOW)['clients']], [True, False])
        self.assertEqual([c['allowed'] for c in snapshot.load(self.directory, NOW + 61)['clients']], [False, False])
        self.assertIsNotNone(snapshot.age_seconds(self.directory))
        for broken in (b'{', snapshot.encode(image(schema=2)), snapshot.encode(image(clients=value['clients'][:1] * 2)),
                       b' ' * (snapshot.MAX_BYTES + 1)):
            (self.directory / snapshot.NAME).write_bytes(broken)
            self.assertIsNone(snapshot.load(self.directory, NOW))
            self.assertIsNone(snapshot.age_seconds(self.directory))

    def database(self):
        path = self.directory / 'db.sqlite'
        db = sqlite3.connect(path)
        db.executescript('''
            CREATE TABLE clients (id INTEGER, name, tg_chat_id, frozen_manual, expires_at, deleting, access_version);
            CREATE TABLE devices (id INTEGER, client_id, name, deleting);
            CREATE TABLE access_operations (client_id, version, status, payload);
            INSERT INTO clients VALUES (1,'Аня','101',0,NULL,0,3), (2,'Боря',NULL,1,NULL,0,1),
                                       (3,'Вера','103',0,1700000000,0,1), (4,'Гена','104',0,NULL,0,2);
            INSERT INTO devices VALUES (11,1,'телефон',0), (12,1,'старый',1), (41,4,'пк',0);
            INSERT INTO access_operations VALUES (1,3,'confirmed','{"enabled":true}'),
                (1,2,'confirmed','{"enabled":false}'), (2,1,'confirmed','{"enabled":true}'),
                (3,1,'confirmed','{"enabled":true}'), (4,2,'pending','{"enabled":true}');
        ''')
        db.commit()
        db.close()
        (self.directory / 'telegram.json').write_text(json.dumps({'bot_token': '1:token', 'chat_id': '900'}))
        return path

    def test_sender_collects_panel_rights_and_delivers_whole_file(self):
        path = self.database()
        config = {'state_dir': self.directory / 'sender',
                  'agent': {'installation_id': 'inst', 'entry_id': 'entry',
                            'nodes': {'entry': {'host': '192.0.2.1'}, 'a': {'host': '192.0.2.10'},
                                      'b': {'host': '192.0.2.11'}}}}
        delivered, down = {}, {'b'}

        def deliver(exit_id, raw):
            if exit_id in down:
                return False
            delivered.setdefault(exit_id, []).append(raw)
            return True

        def collector(now):
            return snapshot_sender.collect(path, self.directory / 'telegram.json', now)

        # Состояние отправителя v1.1.5: та же установка, другая схема.
        (self.directory / 'sender').mkdir()
        (self.directory / 'sender/sender.json').write_text(json.dumps({'installation_id': 'inst', 'snapshot': {}}))
        self.assertEqual(snapshot_sender.run_once(config, deliver, collector, NOW), {'a': 'accepted', 'b': 'unavailable'})
        sent = snapshot.validate(json.loads(delivered['a'][0]))
        self.assertEqual((sent['version'], sent['admin_chat_id'], [e['id'] for e in sent['exits']]), (1, 900, ['a', 'b']))
        self.assertEqual({c['id']: c['allowed'] for c in sent['clients']}, {1: True, 2: False, 3: False, 4: False})
        self.assertEqual(sent['clients'][0]['devices'], [{'id': 11, 'name': 'телефон'}])
        down.clear()
        self.assertEqual(snapshot_sender.run_once(config, deliver, collector, NOW + 5), {'a': 'unchanged', 'b': 'accepted'})
        # Без правок версия та же, файл раз в минуту кладётся заново.
        snapshot_sender.run_once(config, deliver, collector, NOW + snapshot_sender.RECONCILE_SECONDS + 5)
        self.assertEqual(delivered['a'][0], delivered['a'][1])
        # Перепроверка подтверждённой операции доступ не снимает, новая правка - снимает.
        db = sqlite3.connect(path)
        db.execute("UPDATE access_operations SET status='pending' WHERE client_id=1 AND version=3")
        db.commit()
        snapshot_sender.run_once(config, deliver, collector, NOW + 200)
        self.assertEqual(len(delivered['a']), 3)
        db.execute('UPDATE clients SET access_version=4 WHERE id=1')
        db.commit()
        db.close()
        snapshot_sender.run_once(config, deliver, collector, NOW + 210)
        last = json.loads(delivered['a'][-1])
        self.assertEqual((last['version'], last['clients'][0]['allowed']), (2, False))

    def test_push_runs_one_ssh_command_that_cannot_leave_a_cut_file(self):
        calls = self.directory / 'calls'
        ssh = self.directory / 'ssh'
        ssh.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {calls}\ncat > {calls}.body\n')
        ssh.chmod(0o755)
        self.assertTrue(snapshot_sender.push([str(ssh)], 'exit1', b'{"a":1}'))
        lines = calls.read_text().splitlines()
        self.assertIn('exit1', lines)
        self.assertIn('= 7 ] && mv "$f.new" "$f"', lines[-1])
        self.assertIn('--reuid=vpn2-emergency', lines[-1])
        self.assertEqual(Path(f'{calls}.body').read_bytes(), b'{"a":1}')
        self.assertFalse(snapshot_sender.push(['/bin/false'], 'exit1', b'{}'))


class Api:
    def __init__(self):
        self.calls, self.replies = [], []

    def call(self, chat, part):
        self.calls.append((chat, part.get('text')))
        return self.replies.pop(0) if self.replies else Reply(True, result={'message_id': len(self.calls)})


class Spool(Temp):
    def put(self, key, chat=1, parts=2, **meta):
        return spool.put(self.directory, key, chat, [{'method': 'sendMessage', 'text': f'{key}/{i}'}
                                                     for i in range(parts)], **meta)

    def test_same_key_is_sent_once_and_survives_restart_mid_message(self):
        api = Api()
        self.assertTrue(self.put('a'))
        self.assertFalse(self.put('a'))
        api.replies = [Reply(True, result={'message_id': 7}), Reply(False, 'Telegram недоступен или ответ не подтверждён')]
        self.assertFalse(spool.flush(self.directory, api, NOW))
        row = spool.rows(self.directory)[0]
        self.assertEqual((row['state'], row['sent'], row['results']), ('retry', 1, [7]))
        self.assertTrue(spool.flush(self.directory, api, NOW + 1))
        self.assertEqual(len(api.calls), 2)
        self.assertTrue(spool.flush(self.directory, api, NOW + spool.RETRY_FIRST))
        self.assertEqual([text for _, text in api.calls], ['a/0', 'a/1', 'a/1'])
        self.assertEqual(spool.rows(self.directory)[0]['state'], 'sent')
        spool.flush(self.directory, api, NOW + 999)
        self.assertEqual(len(api.calls), 3)

    def test_order_inside_chat_refusals_and_backoff(self):
        api = Api()
        self.put('first', chat=1, parts=1)
        self.put('second', chat=1, parts=1)
        self.put('other', chat=2, parts=1)
        self.put('blocked', chat=3, reason='клиент заморожен', client=5)
        api.replies = [Reply(False, 'Forbidden: bot was blocked by the user', final=True)]
        self.assertTrue(spool.flush(self.directory, api, NOW))
        self.assertEqual(api.calls, [(1, 'first/0'), (2, 'other/0')])
        states = {row['key']: row['state'] for row in spool.rows(self.directory)}
        self.assertEqual(states, {'first': 'failed', 'second': 'pending', 'other': 'sent', 'blocked': 'blocked'})
        self.assertEqual(spool.rows(self.directory, client=5)[0]['reason'], 'клиент заморожен')
        api.replies = [Reply(False, 'Too Many Requests', retry_after=400)]
        self.assertFalse(spool.flush(self.directory, api, NOW + 1))
        spool.flush(self.directory, api, NOW + 399)
        self.assertEqual(len(api.calls), 3)
        spool.flush(self.directory, api, NOW + 402)
        self.assertEqual(api.calls[-1], (1, 'second/0'))

    def test_cancel_and_clear(self):
        self.put('old', emergency='e1')
        self.put('new', emergency='e2')
        spool.cancel(self.directory, emergency='e1')
        api = Api()
        spool.flush(self.directory, api, NOW)
        self.assertEqual({text for _, text in api.calls}, {'new/0', 'new/1'})
        spool.clear(self.directory, keep_emergency='e2')
        self.assertEqual([row['key'] for row in spool.rows(self.directory)], ['new'])


class Notices(Temp):
    common = dict(emergency='e', generation=1, exit_name='Амстердам', wall=NOW)

    def test_client_gets_every_device_or_nothing_with_reason(self):
        value = image()
        value['clients'][0]['devices'].append(dict(id=12, name='ноутбук'))
        value['clients'] += [
            dict(id=2, name='без чата', telegram_chat_id=None, allowed=True, expires_at=None, devices=[dict(id=21, name='x')]),
            dict(id=3, name='истёк', telegram_chat_id=103, allowed=False, expires_at=snapshot.iso_time(NOW - 1),
                 devices=[dict(id=31, name='x')]),
            dict(id=4, name='без устройств', telegram_chat_id=104, allowed=True, expires_at=None, devices=[]),
            dict(id=5, name='без профиля', telegram_chat_id=105, allowed=True, expires_at=None, devices=[dict(id=51, name='x')]),
        ]
        profiles = {11: {'uri': 'hysteria2://a'}, 12: {'uri': 'hysteria2://' + 'b' * 5000}, 21: {'uri': 'hysteria2://c'}}
        notices.clients(self.directory, value, 'initial', profiles=profiles, url='https://192.0.2.10', **self.common)
        rows = {row['client']: row for row in spool.rows(self.directory)}
        self.assertEqual({k: (v['state'], v['reason']) for k, v in rows.items()}, {
            1: ('pending', ''), 2: ('blocked', notices.NO_CHANNEL), 3: ('blocked', 'срок доступа истёк'),
            4: ('blocked', 'нет устройств'), 5: ('blocked', 'нет аварийного профиля устройства')})
        api = Api()
        api.call = lambda _chat, part: (api.calls.append(part), Reply(True))[1]
        spool.flush(self.directory, api, NOW)
        self.assertEqual([part['method'] for part in api.calls], ['sendMessage'] * 3 + ['sendDocument'])
        self.assertEqual(api.calls[1]['text'], 'hysteria2://a' + notices.footer('Амстердам', NOW))
        self.assertTrue(api.calls[0]['text'].endswith('(UTC+03:00)'))
        self.assertEqual(notices.informed(self.directory, 'e'), {1})
        notices.clients(self.directory, value, 'recovered', deadline='12:00', only={1}, **self.common)
        spool.flush(self.directory, api, NOW)
        self.assertEqual(notices.informed(self.directory, 'e'), set())

    def test_long_text_is_split_on_utf16_units_and_no_token_blocks(self):
        parts = notices.split('😀' * 3000, notices.footer('x', NOW))
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(notices.utf16_len(part['text']) <= notices.LIMIT for part in parts))
        notices.admin(self.directory, image(bot_token=None), 'initial', 'текст', **self.common)
        self.assertEqual(spool.rows(self.directory)[0]['reason'], notices.NO_TOKEN)


class Runtime:
    def __init__(self):
        self.actions, self.ok = [], True

    def service(self, action):
        self.actions.append(action)

    def probe(self, _state_dir):
        return self.ok


class Hy2(Temp):
    def setUp(self):
        super().setUp()
        self.options = {'server_ip': '192.0.2.10', 'port': 18443, 'tls_dir': str(self.directory / 'no-tls'),
                        'runtime_dir': str(self.directory / 'hy2')}
        self.runtime = Runtime()
        self.devices = [{'id': 12, 'name': 'ноутбук Ани'}, {'id': 11, 'name': 'телефон'}]

    def start(self, generation, devices=None):
        return hy2.start(self.directory, generation, devices or self.devices, options=self.options,
                         runtime=self.runtime)

    def test_issuance_profiles_server_config_and_resume(self):
        issued = self.start(3)
        home = self.directory / 'hy2'
        server = json.loads((home / 'server.json').read_text())
        users = {user['name']: user['password'] for user in server['inbounds'][0]['users']}
        self.assertEqual(set(users), {'g3-d11', 'g3-d12', 'g3-readiness'})
        self.assertEqual(len(set(users.values())), 3)
        uri = urlsplit(issued[12]['uri'])
        query = parse_qs(uri.query)
        self.assertEqual((uri.scheme, uri.username, uri.hostname, uri.port), ('hysteria2', users['g3-d12'], '192.0.2.10', 18443))
        # Самоподписанный запасной сертификат: клиент сверяет ровно этот лист.
        self.assertEqual((query['insecure'], len(query['pinSHA256'][0]), query['sni']), (['1'], 64, ['192.0.2.10']))
        self.assertTrue(issued[11]['qr_png'].startswith(b'\x89PNG'))
        self.assertEqual(Path(issued[11]['file']).read_text().strip(), issued[11]['uri'])
        self.assertIn({'ip_is_private': True, 'action': 'reject'}, server['route']['rules'])
        self.assertEqual(server['experimental']['clash_api']['external_controller'], '127.0.0.1:18443')
        self.assertEqual(self.runtime.actions, ['stop', 'restart'])
        # Рестарт агента в той же выдаче учётки не меняет; новая выдача меняет.
        self.assertEqual(self.start(3)[11]['uri'], issued[11]['uri'])
        self.assertEqual(self.runtime.actions[-1], 'start')
        self.assertNotEqual(self.start(4)[11]['uri'], issued[11]['uri'])
        hy2.stop(self.directory, runtime=self.runtime)
        self.assertEqual(hy2.read_state(home), {'generation': 4, 'active': False})
        self.assertFalse(list(home.glob('device-*')) or (home / 'server.json').exists())

    def test_failed_self_test_leaves_nothing_issued(self):
        self.runtime.ok = False
        with self.assertRaises(RuntimeError):
            self.start(1)
        self.assertFalse(hy2.read_state(self.directory / 'hy2')['active'])
        self.assertEqual(self.runtime.actions[-1], 'stop')
        with self.assertRaises(ValueError):
            self.start(2, self.devices + self.devices[:1])

    def test_connection_states_come_from_live_sessions_of_this_generation(self):
        self.start(5)
        clock = types.SimpleNamespace(now=NOW)
        poll = lambda live: hy2.poll_sessions(self.directory, fetch=lambda _state: live, clock=lambda: clock.now)  # noqa: E731
        self.assertEqual(hy2.connections(self.directory, 5, clock=lambda: clock.now), {})
        poll({11, 99})
        self.assertEqual(hy2.connections(self.directory, 5, clock=lambda: clock.now), {11: 'connected', 12: 'never'})
        poll(set())
        self.assertEqual(hy2.connections(self.directory, 5, clock=lambda: clock.now), {11: 'seen', 12: 'never'})
        self.assertEqual(hy2.connections(self.directory, 4, clock=lambda: clock.now), {})
        clock.now += hy2.SESSIONS_STALE + 1
        self.assertEqual(hy2.connections(self.directory, 5, clock=lambda: clock.now), {})

    def test_live_devices_are_read_from_the_instance_clash_api(self):
        rules = ['auth_user=g5-d11 => route(internet)', 'auth_user=g4-d12 => route(internet)',
                 'auth_user=g5-d13 => route(internet)', 'ip_is_private=true => reject']
        body = json.dumps({'connections': [{'id': str(i), 'rule': r} for i, r in enumerate(rules)]},
                          separators=(',', ':')).encode()
        seen = []

        class Clash(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append((self.path, self.headers['Authorization']))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Clash)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        state = {'generation': 5, 'api_secret': 'secret', 'options': {'port': server.server_address[1]}}
        self.assertEqual(hy2.live_devices(state), {11, 13})
        self.assertEqual(seen, [('/connections', 'Bearer secret')])

    @unittest.skipUnless(shutil.which('sing-box') and shutil.which('curl'), 'needs sing-box and curl')
    def test_self_test_passes_on_204_and_fails_on_http_error_through_the_tunnel(self):
        class Target(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(404 if self.path == '/missing' else 204)
                self.end_headers()

            def log_message(self, *_args):
                pass

        target = ThreadingHTTPServer(('127.0.0.1', 0), Target)
        threading.Thread(target=target.serve_forever, daemon=True).start()
        self.addCleanup(target.server_close)
        self.addCleanup(target.shutdown)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind(('127.0.0.1', 0))
            port = udp.getsockname()[1]
        home = self.directory / 'hy2'
        home.mkdir()
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                        '-nodes', '-days', '30', '-subj', '/CN=t', '-addext', 'subjectAltName=IP:127.0.0.1',
                        '-keyout', str(home / 'privkey.pem'), '-out', str(home / 'fullchain.pem')],
                       check=True, capture_output=True)
        server = {'log': {'level': 'error'}, 'outbounds': [{'type': 'direct'}],
                  'inbounds': [{'type': 'hysteria2', 'listen': '127.0.0.1', 'listen_port': port,
                                'users': [{'name': 'g1-readiness', 'password': 'probe'}],
                                'tls': {'enabled': True, 'certificate_path': str(home / 'fullchain.pem'),
                                        'key_path': str(home / 'privkey.pem')}}]}
        (self.directory / 'server.json').write_text(json.dumps(server))
        instance = subprocess.Popen([shutil.which('sing-box'), 'run', '-c', str(self.directory / 'server.json')])
        self.addCleanup(instance.wait)
        self.addCleanup(instance.terminate)
        for path, expected in (('/generate_204', True), ('/missing', False)):
            options = {'port': port, 'server_ip': '127.0.0.1', 'binary': shutil.which('sing-box'),
                       'probe_url': f'http://127.0.0.1:{target.server_address[1]}{path}'}
            (home / 'generation.json').write_text(json.dumps(
                {'generation': 1, 'active': True, 'probe_password': 'probe', 'options': options}))
            with self.subTest(path=path):
                self.assertIs(hy2.probe(self.directory), expected)

    def test_firewall_isolates_the_service_user_and_gates_the_port(self):
        closed, opened = hy2.firewall_rules(990, 18443, False), hy2.firewall_rules(990, 18443, True)
        self.assertIn('udp dport 18443 reject', closed)
        self.assertNotIn('udp dport 18443 reject', opened)
        for rule in ('meta skuid 990 fib daddr type local reject', '10.0.0.0/8', 'fc00::/7'):
            self.assertIn(rule, opened)
        with self.assertRaises(ValueError):
            hy2.firewall_rules(0, 18443, True)

    def test_endpoint_is_frozen_while_emergency_is_active(self):
        path = self.directory / 'options.json'
        hy2.endpoint('192.0.2.10', 18443, False, path)
        hy2.endpoint('192.0.2.10', 18443, True, path)
        with self.assertRaises(SystemExit):
            hy2.endpoint('192.0.2.10', 18444, True, path)
        hy2.endpoint('192.0.2.10', 18444, False, path)
        self.assertEqual(json.loads(path.read_text())['port'], 18444)


if __name__ == '__main__':
    unittest.main()
