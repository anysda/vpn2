"""Emergency lifecycle on one exit: real leader, queue and panel; fake Hysteria2.

The same assertions can target an older tree using VPN2_TEST_ROOT.
"""

import functools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import math
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'infra/emergency'))

from vpn2_emergency import hooks, hy2, tgqueue, web  # noqa: E402
from vpn2_emergency.leader import GUARD, STABLE, Leader  # noqa: E402
from vpn2_emergency.state import State  # noqa: E402
from vpn2_emergency import lifecycle  # noqa: E402

WALL = 1_800_000_000.0
EXITS = [{'id': 'a', 'name': 'exit-a', 'priority': 0, 'ip': '203.0.113.10'}]


def load_snapshot(state_dir):
    return json.loads((Path(state_dir) / 'fixture.json').read_text())


def snapshot_stubs():
    """The panel imports the snapshot module lazily: an already imported real
    module is a package attribute and wins over sys.modules."""
    module = types.ModuleType('vpn2_emergency.snapshot')
    module.load = load_snapshot
    return (patch.dict(sys.modules, {'vpn2_emergency.snapshot': module}),
            patch.object(sys.modules['vpn2_emergency'], 'snapshot', module, create=True))


class Bot(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(('127.0.0.1', 0), BotHandler)
        self.texts = []
        self.down = False


class BotHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):  # noqa - HTTP handler
        pass

    def do_POST(self):  # noqa - HTTP handler
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        code = 502 if self.server.down else 200
        if not self.server.down:
            self.server.texts.append((data['chat_id'], data['text']))
        self.send_response(code)
        self.end_headers()
        self.wfile.write(json.dumps({'ok': code == 200, 'error_code': code}).encode())


class FakeHy2:
    def __init__(self):
        self.starts = []
        self.stops = 0
        self.running = False

    def start(self, _state_dir, generation, devices, options=None):
        del options
        self.starts.append((generation, [device['id'] for device in devices]))
        self.running = True
        return {device['id']: {'uri': f"hysteria2://{device['id']}@203.0.113.10:18443/"}
                for device in devices}

    def stop(self, _state_dir):
        self.stops += 1
        self.running = False

    def settings(self, _options):
        return {'port': 18443}

    def listening(self, _port):
        return self.running

    def watch(self, _state_dir):
        return types.SimpleNamespace(stop=lambda: None)

    def connections(self, _state_dir, _generation):
        return {}

    def revoked(self, _state_dir, _generation):
        return False

    def refresh_tls(self, _state_dir):
        return False


class FakeServer:
    def __init__(self, panel):
        self.panel = panel
        self.closed = False

    def serve_forever(self):
        pass

    def shutdown(self):
        self.closed = True

    def server_close(self):
        pass

    def refresh_tls(self):
        return False


class Agent:
    """The fields of vpn2_emergency.agent.Agent the lifecycle reads."""

    def __init__(self, directory, wall):
        self.state = State(directory)
        self.node = 'a'
        self.entry = 'entry'
        self.addresses = {'a': ('203.0.113.10', 51821), 'entry': ('203.0.113.1', 51821)}
        self.config = {'recovery': {}}
        self.wall = wall
        self.snapshot_loader = load_snapshot
        self.ready = False
        self.entry_check = None
        self.leader = Leader(self.state, 'a', EXITS, 'fixture', 0, events=hooks, wall=wall)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='lifecycle-')
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.snapshot = {'schema': 1, 'installation_id': 'fixture', 'bot_token': '123:fixture-only',
                         'admin_chat_id': 900, 'exits': EXITS,
                         'created_at': '2026-01-01T00:00:00Z',
                         'clients': [
                             {'id': 1, 'name': 'first', 'telegram_chat_id': 101, 'allowed': True,
                              'expires_at': None, 'devices': [{'id': 11, 'name': 'phone'}]},
                             {'id': 2, 'name': 'second', 'telegram_chat_id': 102, 'allowed': True,
                              'expires_at': None, 'devices': [{'id': 22, 'name': 'laptop'}]}]}
        self.save()
        for stub in snapshot_stubs():
            stub.start()
            self.addCleanup(stub.stop)
        self.bot = Bot()
        thread = threading.Thread(target=self.bot.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        self.addCleanup(self.bot.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.bot.shutdown)
        self.addCleanup(hooks._listeners.clear)
        self.wall = WALL
        self.now = 0.0
        self.hy2 = FakeHy2()
        self.servers = []
        self.agent = Agent(self.directory, lambda: self.wall)
        self.lifecycle = self.make()

    def save(self):
        (self.directory / 'fixture.json').write_text(json.dumps(self.snapshot))

    def make_server(self, state_dir, *, exit_id, public_ip, address, **providers):
        del address
        server = FakeServer(web.Panel(state_dir, exit_id, public_ip, **providers))
        self.servers.append(server)
        return server

    def make(self):
        hooks._listeners.clear()
        cycle = lifecycle.Lifecycle(
            self.agent, hy2_module=self.hy2, web_module=types.SimpleNamespace(make_server=self.make_server),
            threaded=False, api_base=f'http://127.0.0.1:{self.bot.server_port}',
            probe_ready=lambda: True, probe_entry=lambda _host: True)
        cycle.attach()
        return cycle

    def advance(self, seconds, ready=True):
        for _ in range(int(seconds // 15)):
            self.now += 15
            self.wall += 15
            leader = self.agent.leader
            # The entry answering at all ends the outage; readiness is separate.
            leader.tick(self.now, ready is None)
            leader.entry_state(self.now, ready)
            self.lifecycle.tick(self.now)

    def lead(self):
        self.advance(GUARD + 60, ready=None)
        self.assertEqual(self.agent.leader.data['role'], 'leader')
        self.assertTrue(self.agent.leader.data['ready'])
        self.bot.texts.clear()

    def recover(self):
        self.lead()
        self.advance(STABLE + 15)
        recovery = self.agent.leader.data['recovery']
        self.assertIsNotNone(recovery)
        return recovery

    def recovered_texts(self):
        return [text for _chat, text in self.bot.texts if 'отключ' in text]

    def test_leader_issues_hysteria2_panel_and_notices(self):
        self.lead()
        self.assertEqual(self.hy2.starts, [(1, [11, 22])])
        self.assertTrue(self.servers[0].panel.auth)
        rows = tgqueue.delivery_status(self.directory)
        self.assertEqual({row['event'] for row in rows}, {'initial'})

    def test_follower_serves_instructions_and_sends_the_rest_to_the_leader(self):
        # VPN2-518: :443 answers on every exit during an emergency.
        exits = [*EXITS, {'id': 'b', 'name': 'exit-b', 'priority': 1, 'ip': '203.0.113.11'}]
        self.snapshot['exits'] = exits
        self.save()
        static = self.directory / 'static'
        static.mkdir()
        (static / 'instructions.html').write_bytes((ROOT / 'app/emergency/instructions.html').read_bytes())
        self.agent.leader = Leader(self.agent.state, 'a', exits, 'fixture', 0, events=hooks, wall=self.agent.wall)
        hooks._listeners.clear()
        make_server = functools.partial(web.make_server, static_dir=static, cert_dir=self.directory / 'no-tls')
        cycle = lifecycle.Lifecycle(
            self.agent, hy2_module=self.hy2, web_module=types.SimpleNamespace(make_server=make_server),
            threaded=False, panel_address=('127.0.0.1', 0), api_base=f'http://127.0.0.1:{self.bot.server_port}',
            probe_ready=lambda: True, probe_entry=lambda _host: True)
        cycle.attach()
        self.addCleanup(cycle.close)

        def get(path):
            connection = http.client.HTTPSConnection(
                '127.0.0.1', cycle.server.server_address[1], timeout=5,
                context=ssl._create_unverified_context())  # noqa: S323 - local self-signed panel
            try:
                connection.request('GET', path)
                response = connection.getresponse()
                return response.status, response.getheader('Location'), response.read()
            finally:
                connection.close()

        data = self.agent.leader.data
        for emergency in ('first', 'second'):
            data.update(active=True, role='follower', owner='b', emergency_id=emergency)
            cycle.tick(self.now)
            self.assertIsNotNone(cycle.server, emergency)
            status, _location, body = get('/instructions')
            self.assertEqual(status, 200)
            self.assertEqual(body, (static / 'instructions.html').read_bytes())
            for path in ('/', '/api/state'):
                self.assertEqual(get(path)[:2], (307, 'https://203.0.113.11/'))
            # A follower issues nothing of its own.
            self.assertEqual(self.hy2.starts, [])
            self.agent.leader.finish('test')
            self.assertIsNone(cycle.server)

    def test_panel_broadcast_reaches_clients_through_the_leader_queue(self):
        # VPN2-519: the panel the lifecycle builds can send the admin's text.
        self.lead()
        panel = self.servers[0].panel
        self.assertTrue(panel.view()['bot_ready'])
        generation = panel.auth['generation']
        panel.broadcast('Проверка связи', generation)
        panel.broadcast('Проверка связи', generation)
        self.advance(30)
        texts = [(chat, text) for chat, text in self.bot.texts if text.startswith('Проверка связи')]
        # The same text twice is two messages; the footer names the node.
        self.assertEqual(sorted(chat for chat, _text in texts), [101, 101, 102, 102])
        self.assertTrue(all('Узел: exit-a' in text for _chat, text in texts))
        self.assertEqual(panel.view()['delivery']['pending'], 0)
        # A client without access gets nothing and the admin sees why.
        self.snapshot['clients'][1]['allowed'] = False
        self.save()
        panel.broadcast('Второе', generation)
        self.advance(15)
        self.assertEqual([chat for chat, text in self.bot.texts if text.startswith('Второе')], [101])
        self.assertEqual(panel.view()['delivery']['denied'], 1)

    def test_deadline_is_fixed_by_first_delivery_and_shown_on_panel(self):
        recovery = self.recover()
        self.advance(15)
        deadline = self.agent.leader.data['recovery']['deadline']
        self.assertIsNotNone(deadline)
        self.assertEqual(deadline % 60, 0)
        self.assertLessEqual(recovery['confirmed'] + 600, deadline)
        self.assertLess(deadline, self.wall + 600 + 60)
        human = tgqueue._human(lifecycle.iso(deadline))
        self.assertIn('UTC+03:00', human)
        chats = {chat for chat, text in self.bot.texts if human in text}
        self.assertEqual(chats, {900, 101, 102})
        view = self.servers[0].panel.view()
        self.assertEqual(view['deadline'], deadline)
        self.assertIsNotNone(view['remaining'])
        # No new bundle on recovery.
        self.assertEqual(len(self.hy2.starts), 1)

    def test_telegram_down_waits_then_deadline_counts_from_delivery(self):
        self.bot.down = True
        self.recover()
        self.advance(3600)
        self.assertIsNone(self.agent.leader.data['recovery']['deadline'])
        self.assertTrue(self.agent.leader.data['active'])
        self.bot.down = False
        unblocked = self.wall
        while self.agent.leader.data['recovery']['deadline'] is None:
            self.advance(15)
            self.assertLess(self.wall, unblocked + 3600)
        deadline = self.agent.leader.data['recovery']['deadline']
        self.assertTrue(self.recovered_texts())
        # Retries back off, yet the ten minutes count from the delivery itself.
        self.assertGreaterEqual(deadline, unblocked + 600)
        self.assertLessEqual(deadline, self.wall + 600 + 60)
        self.assertLessEqual(self.wall, unblocked + 90)
        self.assertTrue(self.agent.leader.data['active'])

    def test_step_down_cancels_unsent_notices_and_keeps_running(self):
        self.bot.down = True
        self.lead()
        self.agent.leader._step_down('b')
        self.lifecycle.tick(self.now)
        rows = tgqueue.delivery_status(self.directory) + tgqueue.delivery_status(self.directory, 'admin')
        self.assertTrue(rows)
        self.assertEqual({row['state'] for row in rows}, {'cancelled'})
        self.bot.down = False
        self.advance(60, ready=None)
        self.assertEqual(self.bot.texts, [])

    def test_telegram_down_two_hours_ends_without_notice(self):
        self.bot.down = True
        self.recover()
        with self.assertLogs(level='INFO') as logs:
            self.advance(7200)
        data = self.agent.leader.data
        self.assertFalse(data['active'])
        self.assertEqual(data['role'], 'observer')
        self.assertTrue(any('2 часа' in line for line in logs.output))
        self.assertEqual(self.hy2.stops, 1)
        self.assertFalse(self.servers[0].panel.auth)
        self.assertEqual(self.recovered_texts(), [])

    def test_no_bot_token_counts_from_confirmation(self):
        del self.snapshot['bot_token']
        self.save()
        recovery = self.recover()
        self.advance(15)
        self.assertEqual(self.agent.leader.data['recovery']['deadline'],
                         math.ceil((recovery['confirmed'] + 600) / 60) * 60)

    def test_deadline_revokes_access_closes_port_and_panel(self):
        self.recover()
        self.advance(15)
        with self.assertLogs(level='INFO') as logs:
            self.advance(660)
        self.assertFalse(self.agent.leader.data['active'])
        self.assertEqual(self.hy2.stops, 1)
        self.assertFalse(self.hy2.running)
        self.assertTrue(self.servers[0].closed)
        self.assertFalse(self.servers[0].panel.auth)
        self.assertTrue(any('port 18443 closed' in line for line in logs.output))
        self.assertTrue(any('emergency ended' in line for line in logs.output))
        self.assertEqual(self.agent.state.data['lifecycle'], {})

    def test_return_cancelled_keeps_access_and_tells_those_notified(self):
        self.recover()
        self.advance(15)
        self.bot.texts.clear()
        self.advance(30, ready=False)
        self.advance(15, ready=False)
        data = self.agent.leader.data
        self.assertIsNone(data['recovery'])
        self.assertTrue(data['active'])
        self.assertEqual(self.hy2.stops, 0)
        self.assertTrue(self.servers[0].panel.view()['cancelled'])
        self.assertEqual({chat for chat, text in self.bot.texts if 'отмен' in text.lower()}, {900, 101, 102})
        self.advance(900, ready=False)
        self.assertTrue(data['active'])

    def test_restart_restores_issuance_without_new_messages(self):
        self.recover()
        self.advance(15)
        deadline = self.agent.leader.data['recovery']['deadline']
        sent = len(self.bot.texts)
        self.agent.state.save()
        wall = self.agent.wall
        self.agent.leader = Leader(self.agent.state, 'a', EXITS, 'fixture', self.now, events=hooks, wall=wall)
        self.lifecycle = self.make()
        self.advance(30)
        self.assertEqual(self.agent.leader.data['recovery']['deadline'], deadline)
        self.assertEqual(self.hy2.starts[-1][0], 1)
        self.assertEqual(len(self.bot.texts), sent)

    def test_reboot_resumes_issuance_without_leader_changed_notice(self):
        self.lead()
        sent = len(self.bot.texts)
        self.agent.state.data['clock_boot'] = 'other'
        self.agent.state.save()
        self.agent.state = State(self.directory)
        self.agent.leader = Leader(self.agent.state, 'a', EXITS, 'fixture', self.now, events=hooks,
                                   wall=self.agent.wall)
        self.assertEqual(self.agent.leader.data['role'], 'observer')
        self.lifecycle = self.make()
        self.advance(GUARD + 60, ready=None)
        self.assertEqual(self.agent.leader.data['role'], 'leader')
        self.assertEqual(self.hy2.starts[-1][0], 1)
        self.assertEqual(len(self.bot.texts), sent)
        events = {row['event'] for row in tgqueue.delivery_status(self.directory)}
        self.assertEqual(events, {'initial'})

    def test_new_entry_address_is_reported_to_admin(self):
        self.lead()
        self.agent.state.data['entry_moved'] = {'from': ['203.0.113.1', 51821], 'to': ['198.51.100.7', 51821],
                                                'emergency': self.agent.leader.data['emergency_id']}
        self.advance(STABLE + 30)
        admin = [text for chat, text in self.bot.texts if chat == 900]
        self.assertTrue(any('198.51.100.7' in text for text in admin))
        self.assertFalse(any('198.51.100.7' in text for chat, text in self.bot.texts if chat != 900))

    def test_exit_check_failure_blocks_readiness(self):
        self.agent.node = 'b'
        self.agent.addresses['b'] = ('203.0.113.11', 51821)
        cycle = lifecycle.Lifecycle(self.agent, hy2_module=self.hy2, threaded=False,
                                    probe_entry=lambda _host: False)
        self.agent.leader.data['active'] = True
        cycle.tick(100)
        cycle.tick(115)
        self.assertEqual(self.agent.entry_check, (False, 100))


class Clash(ThreadingHTTPServer):
    """Session API of the emergency sing-box: one live stream per listed user."""

    def __init__(self):
        super().__init__(('127.0.0.1', 0), ClashHandler)
        self.users = []
        self.secret = None


class ClashHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):  # noqa - HTTP handler
        pass

    def do_GET(self):  # noqa - HTTP handler
        if self.path != '/connections' or self.headers['Authorization'] != f'Bearer {self.server.secret()}':
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps({'connections': [
            {'id': str(index), 'rule': f'auth_user={user} => route(internet)'}
            for index, user in enumerate(self.server.users)]}, separators=(',', ':')).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def openssl(*args):
    subprocess.run(['openssl', *args], check=True, capture_output=True)


class Renewals:
    """A local CA and the common TLS directory as vpn2_tls publishes it:
    fullchain.pem and privkey.pem point into current/, one symlink switch per renewal."""

    def __init__(self, directory, ip):
        self.directory, self.ip = directory, ip
        self.ca, self.ca_key = directory / 'ca.pem', directory / 'ca.key'
        openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                '-days', '1', '-subj', '/CN=fixture CA', '-keyout', str(self.ca_key), '-out', str(self.ca))
        self.output = directory / 'tls'
        self.output.mkdir()
        self.count = 0

    def renew(self):
        self.count += 1
        generation = self.output / f'g{self.count}'
        generation.mkdir()
        cert, key, request = generation / 'fullchain.pem', generation / 'privkey.pem', generation / 'request'
        extensions = self.directory / 'extensions'
        extensions.write_text(f'subjectAltName=IP:{self.ip}\nextendedKeyUsage=serverAuth\n')
        openssl('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                '-subj', f'/CN={self.ip}', '-keyout', str(key), '-out', str(request))
        openssl('x509', '-req', '-in', str(request), '-CA', str(self.ca), '-CAkey', str(self.ca_key),
                '-CAcreateserial', '-days', '1', '-extfile', str(extensions), '-out', str(cert))
        pending = self.output / 'current.new'
        pending.symlink_to(generation.name)
        pending.replace(self.output / 'current')
        for name in ('fullchain.pem', 'privkey.pem'):
            link = self.output / name
            if not link.is_symlink():
                link.symlink_to(f'current/{name}')
        return ssl.PEM_cert_to_DER_cert(cert.read_text())


class RealHy2Tests(unittest.TestCase):
    """Real lifecycle, Hysteria2 state, session poller and HTTPS panel; fake sing-box."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='indicator-')
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        (self.directory / 'fixture.json').write_text(json.dumps({
            'schema': 1, 'installation_id': 'fixture', 'admin_chat_id': 900, 'exits': EXITS,
            'bot_token': '123:fixture-only',
            'created_at': '2026-01-01T00:00:00Z',
            'clients': [{'id': 1, 'name': 'first', 'telegram_chat_id': None, 'allowed': True,
                         'expires_at': None, 'devices': [{'id': 11, 'name': 'phone'}]},
                        {'id': 2, 'name': 'second', 'telegram_chat_id': None, 'allowed': True,
                         'expires_at': None, 'devices': [{'id': 22, 'name': 'laptop'}]}]}))
        for stub in (*snapshot_stubs(),
                     patch.object(hy2, 'SystemRuntime', Mock),
                     patch.object(hy2, 'selftest', return_value=True),
                     # The agent's poller, only faster than the 12 s of a real exit.
                     patch.object(hy2.SessionWatch.__init__, '__defaults__', (0.05, hy2.live_devices))):
            stub.start()
            self.addCleanup(stub.stop)
        self.clash = Clash()
        self.clash.secret = lambda: hy2.read_state(self.directory / 'hy2').get('api_secret')
        thread = threading.Thread(target=self.clash.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        self.addCleanup(self.clash.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.clash.shutdown)
        self.addCleanup(hooks._listeners.clear)
        self.now, self.wall = 0.0, WALL
        self.agent = Agent(self.directory, lambda: self.wall)
        hooks._listeners.clear()
        self.lifecycle = lifecycle.Lifecycle(
            self.agent, threaded=False, panel_address=('127.0.0.1', 0),
            hy2_options={'server_ip': '203.0.113.10', 'port': self.clash.server_port,
                         'tls_dir': str(self.directory / 'no-tls'),
                         'runtime_dir': str(self.directory / 'hy2')},
            api_base='http://127.0.0.1:9', probe_ready=lambda: True, probe_entry=lambda _host: True)
        self.lifecycle.attach()
        self.addCleanup(self.lifecycle.close)

    def advance(self, seconds, ready=None):
        for _ in range(int(seconds // 15)):
            self.now += 15
            self.wall += 15
            self.agent.leader.tick(self.now, ready is None)
            self.agent.leader.entry_state(self.now, ready)
            self.lifecycle.tick(self.now)

    def request(self, method, path, body=None, cookie=None):
        server = self.lifecycle.server
        connection = http.client.HTTPSConnection(
            '127.0.0.1', server.server_address[1], timeout=5,
            context=ssl._create_unverified_context())  # noqa: S323 - local self-signed panel
        headers = {'Origin': f'https://127.0.0.1:{server.server_address[1]}',
                   'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        try:
            connection.request(method, path, body=json.dumps(body) if body else None, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheader('Set-Cookie'), response.read()
        finally:
            connection.close()

    def statuses(self, expected):
        """Panel /api/state as the admin sees it, until it shows `expected`."""
        status, cookie, _ = self.request('POST', '/api/login',
                                         {'password': self.lifecycle.server.panel.auth['password']})
        self.assertEqual(status, 200)
        deadline = time.monotonic() + 5
        while True:
            status, _, body = self.request('GET', '/api/state', cookie=cookie.split(';')[0])
            self.assertEqual(status, 200)
            seen = {client['id']: client['status'] for client in json.loads(body)['clients']}
            if seen == expected or time.monotonic() > deadline:
                return seen
            time.sleep(0.05)

    def test_connection_states_reach_panel_api(self):
        self.advance(GUARD + 60)
        self.assertEqual(self.agent.leader.data['role'], 'leader')
        self.assertEqual(hy2.read_state(self.directory / 'hy2')['generation'], 1)
        self.clash.users = ['g1-d11']
        self.assertEqual(self.statuses({1: 'connected', 2: 'never'}), {1: 'connected', 2: 'never'})
        self.clash.users = []
        self.assertEqual(self.statuses({1: 'seen', 2: 'never'}), {1: 'seen', 2: 'never'})
        # A new issuance (another leader, lost credentials) starts from scratch;
        # old-generation streams do not count.
        self.clash.users = ['g1-d11']
        self.agent.leader.credentials_lost()
        self.advance(15)
        self.assertEqual(hy2.read_state(self.directory / 'hy2')['generation'], 2)
        self.assertEqual(self.statuses({1: 'never', 2: 'never'}), {1: 'never', 2: 'never'})
        self.clash.users = ['g2-d22']
        self.assertEqual(self.statuses({1: 'never', 2: 'connected'}), {1: 'never', 2: 'connected'})

    def notices(self, audience='admin'):
        return tgqueue.delivery_status(self.directory, audience)

    def test_failing_self_test_keeps_generation_and_announces_once(self):
        with patch.object(hy2, 'selftest', return_value=False) as selftest:
            self.advance(GUARD + 60)
            self.assertEqual(self.agent.leader.data['role'], 'leader')
            self.advance(600)
        self.assertGreaterEqual(selftest.call_count, 4)
        self.assertEqual(self.agent.leader.data['issued_generation'], 1)
        self.assertEqual(hy2.read_state(self.directory / 'hy2')['generation'], 1)
        self.assertEqual(self.notices() + self.notices('client'), [])
        self.assertIsNone(self.lifecycle.server)
        self.advance(300)
        self.assertEqual(self.agent.leader.data['issued_generation'], 1)
        self.assertTrue(self.agent.leader.data['ready'])
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual({row['generation'] for row in self.notices() + self.notices('client')}, {1})
        self.advance(600)
        self.assertEqual(self.agent.leader.data['issued_generation'], 1)
        self.assertEqual(len(self.notices()), 1)

    def test_renewal_during_emergency_reaches_hysteria2_and_panel(self):
        # VPN2-529: the common IP certificate renewed mid-emergency is served
        # by Hysteria2 and the panel; an open panel connection keeps its own.
        renewals = Renewals(self.directory, '203.0.113.10')
        self.lifecycle.close()
        hooks._listeners.clear()
        make_server = functools.partial(web.make_server, cert_dir=renewals.output)
        self.lifecycle = lifecycle.Lifecycle(
            self.agent, threaded=False, panel_address=('127.0.0.1', 0),
            web_module=types.SimpleNamespace(make_server=make_server),
            hy2_options={'server_ip': '203.0.113.10', 'port': self.clash.server_port,
                         'tls_dir': str(renewals.output), 'runtime_dir': str(self.directory / 'hy2')},
            api_base='http://127.0.0.1:9', probe_ready=lambda: True, probe_entry=lambda _host: True)
        self.lifecycle.attach()
        self.addCleanup(self.lifecycle.close)

        def connect():
            # An IP address, as browsers reach the panel: no SNI in the ClientHello.
            raw = socket.create_connection(('127.0.0.1', self.lifecycle.server.server_address[1]), timeout=5)
            return ssl._create_unverified_context().wrap_socket(raw)  # noqa: S323 - certificate is compared below

        def hysteria2():
            return ssl.PEM_cert_to_DER_cert((self.directory / 'hy2/fullchain.pem').read_text())

        with patch.dict(os.environ, {'SSL_CERT_FILE': str(renewals.ca)}):
            first = renewals.renew()
            self.advance(GUARD + 60)
            self.assertEqual(self.agent.leader.data['role'], 'leader')
            self.assertFalse(hy2.read_state(self.directory / 'hy2')['pin'])
            self.assertEqual(hysteria2(), first)
            opened = connect()
            self.addCleanup(opened.close)
            self.assertEqual(opened.getpeercert(binary_form=True), first)
            second = renewals.renew()
            self.advance(75)
        with connect() as fresh:
            served = fresh.getpeercert(binary_form=True)
        self.assertEqual({'hysteria2': hysteria2() == second, 'panel': served == second},
                         {'hysteria2': True, 'panel': True})
        self.assertIsNone(self.lifecycle.server.panel.fingerprint)
        opened.sendall(b'GET /api/state HTTP/1.0\r\n\r\n')
        self.assertTrue(opened.recv(64).startswith(b'HTTP/1.0 401'))
        self.assertEqual(opened.getpeercert(binary_form=True), first)

    def test_poller_stops_with_emergency_end(self):
        self.advance(GUARD + 60)
        poller = self.lifecycle.sessions
        self.assertTrue(poller is not None and poller.is_alive())
        self.lifecycle.on_emergency_end()
        poller.join(5)
        self.assertFalse(poller.is_alive())
        self.assertIsNone(self.lifecycle.sessions)
        self.assertFalse(hy2.read_state(self.directory / 'hy2')['active'])


class ProbeTests(unittest.TestCase):
    def test_entry_ready_requires_every_unit(self):
        def run(_args, **_kwargs):
            return types.SimpleNamespace(stdout='active\ninactive\nactive\nactive\nactive\n')
        with patch.object(lifecycle.subprocess, 'run', run):
            self.assertFalse(lifecycle.entry_ready({}))

    def test_entry_ready_requires_panel(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()

        def run(args, **_kwargs):
            return types.SimpleNamespace(stdout='active\n' * (len(args) - 2))
        with patch.object(lifecycle.subprocess, 'run', run):
            self.assertFalse(lifecycle.entry_ready({'panel': f'http://127.0.0.1:{port}/api/version'}))

    def test_socks_connect_needs_proxy_success(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        self.addCleanup(server.close)
        answers = [b'\5\0\0\1\0\0\0\0\0\0', b'\5\5\0\1\0\0\0\0\0\0']

        def serve():
            for answer in answers:
                conn, _ = server.accept()
                with conn:
                    conn.recv(3)
                    conn.sendall(b'\5\0')
                    conn.recv(64)
                    conn.sendall(answer)
        thread = threading.Thread(target=serve)
        thread.start()
        self.addCleanup(thread.join)
        target = ('example.org', 80)
        self.assertTrue(lifecycle.socks_connect(server.getsockname(), target))
        self.assertFalse(lifecycle.socks_connect(server.getsockname(), target))


if __name__ == '__main__':
    unittest.main(verbosity=2)
