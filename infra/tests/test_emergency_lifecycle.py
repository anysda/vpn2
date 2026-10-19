"""Emergency lifecycle on one exit: real leader, queue and panel; fake Hysteria2.

The same assertions can target an older tree using VPN2_TEST_ROOT.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'infra/emergency'))

from vpn2_emergency import hooks, tgqueue, web  # noqa: E402
from vpn2_emergency.leader import GUARD, STABLE, Leader  # noqa: E402
from vpn2_emergency.state import State  # noqa: E402
from vpn2_emergency import lifecycle  # noqa: E402

WALL = 1_800_000_000.0
EXITS = [{'id': 'a', 'name': 'exit-a', 'priority': 0, 'ip': '203.0.113.10'}]


def load_snapshot(state_dir):
    return json.loads((Path(state_dir) / 'fixture.json').read_text())


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
        module = types.ModuleType('vpn2_emergency.snapshot')
        module.load = load_snapshot
        stub = patch.dict(sys.modules, {'vpn2_emergency.snapshot': module})
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

    def make_server(self, state_dir, *, exit_id, public_ip, address, profiles):
        del address
        server = FakeServer(web.Panel(state_dir, exit_id, public_ip, profiles=profiles))
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
