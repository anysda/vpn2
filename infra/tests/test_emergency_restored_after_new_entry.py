"""«Восстановлено» after an emergency ended by a new entry (docs/emergency.md).

The exits are in an emergency, clients already got emergency profiles, then
the entry is installed anew and restored from backup. A client hears that the
main VPN is back once, and only when the new entry's snapshot has him again:
that is when his previous profile works. Real agents, real snapshot files,
real notice queue against a local Bot API; the same assertions can target an
older tree using VPN2_TEST_ROOT.
"""

import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / 'infra/emergency'))

from vpn2_emergency import lifecycle, snapshot, snapshot_sender, tgqueue  # noqa: E402
from vpn2_emergency.agent import Agent  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    'restored_restore_harness', Path(__file__).resolve().parent / 'test_restore_entry_live_exits.py')
restore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(restore)
harness = restore.harness

NOW = restore.NOW
TOKEN = '123:fixture-only'
RESTORED = 'Основное VPN-подключение восстановлено'
CLIENTS = {1: (101, 11), 2: (102, 22), 3: (None, 33)}


def client(client_id, chat=None, device=None):
    default_chat, default_device = CLIENTS[client_id]
    return {'id': client_id, 'name': f'client-{client_id}',
            'telegram_chat_id': default_chat if chat is None else chat, 'allowed': True,
            'expires_at': None, 'devices': [{'id': device or default_device, 'name': 'phone'}]}


class Bot(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(('127.0.0.1', 0), BotHandler)
        self.calls = []


class BotHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):  # noqa - HTTP handler
        pass

    def do_POST(self):  # noqa - HTTP handler
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(data)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({'ok': True, 'result': {'message_id': 1}}).encode())


class Installation(restore.Installation):
    """Exits after stage 23 run the real notice queue against the local Bot API."""

    def __init__(self, directory, api):
        self.api = api
        super().__init__(directory)

    def image(self, installation, version=1):
        return super().image(installation, version) | {
            'bot_token': TOKEN, 'clients': [client(1), client(2), client(3)]}

    def start(self, node):
        if node == 'entry' or node not in self.hy2:
            return super().start(node)
        agent = Agent(self.configs[node], self.directory / node, clock=self.clock,
                      snapshot_loader=self.snapshot, **self.wall_clock())
        self.agents[node] = agent
        agent.lifecycle = lifecycle.Lifecycle(
            agent, hy2_module=self.hy2[node], web_module=types.SimpleNamespace(make_server=None),
            tg=tgqueue, threaded=False, api_base=self.api, probe_ready=lambda: True,
            probe_entry=lambda _host: True)
        agent.tick(self.clock())
        return None


class TestRestoredAfterNewEntry(unittest.TestCase):
    def setUp(self):
        self.bot = Bot()
        thread = threading.Thread(target=self.bot.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        self.addCleanup(self.bot.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.bot.shutdown)
        self.api = f'http://127.0.0.1:{self.bot.server_port}'
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cluster = Installation(self.root / 'nodes', self.api)
        self.addCleanup(self.cluster.close)
        for item in self.cluster.exits:
            result = snapshot.accept(self.root / 'nodes' / item['id'],
                                     self.cluster.image('fixture'), None, 'fixture', NOW)
            self.assertEqual(result['status'], 'accepted')
        self.cluster.advance(30)
        self.exits, self.sender = restore.Exits(self.cluster), self.root / 'sender'
        self.panel = []

    def collector(self, _moment):
        return {'clients': [dict(item, access_version=1) for item in self.panel],
                'bot_token': TOKEN, 'admin_chat_id': None}

    def send(self, now):
        config = {'state_dir': self.sender, 'agent': self.cluster.configs['entry']}
        return snapshot_sender.run_once(config, self.exits.deliver, self.collector, now=now)

    def profiles_sent(self, node):
        """What the leader's queue does at issuance: profiles to every client."""
        directory = self.cluster.directory / node
        emergency = self.cluster.agents['a'].leader.data['emergency_id']
        profiles = {device: {'uri': f'hysteria2://p{device}@192.0.2.1:18443/'}
                    for _chat, device in CLIENTS.values()}
        tgqueue.enqueue_clients(directory, emergency_id=emergency, generation=1, bundle_version=1,
                                exit_name=node, issued_at=lifecycle.iso(self.cluster.wall()),
                                exit_url='https://192.0.2.1', profiles=profiles, event='initial')
        # One part per chat and pass: the notice, then the profile.
        while tgqueue.run_once(directory, api_base=self.api, now=self.cluster.wall()):
            pass

    def restored(self):
        chats = [call['chat_id'] for call in self.bot.calls if RESTORED in call.get('text', '')]
        return {chat: chats.count(chat) for chat in chats}

    def emergency_then_new_entry(self):
        cluster = self.cluster
        cluster.stop('entry')
        cluster.advance(1320)
        self.assertEqual(cluster.leaders(), ['a'])
        for node in ('a', 'b'):
            cluster.hy2[node] = harness.FakeHy2()
        cluster.hy2['a'].running = True
        # b led this emergency before a and keeps its own delivered rows.
        self.profiles_sent('b')
        self.profiles_sent('a')
        got = [call['chat_id'] for call in self.bot.calls if 'hysteria2://' in call.get('text', '')]
        self.assertEqual(sorted(got), [101, 101, 102, 102])
        cluster.new_entry('new', 'cd' * 32)
        self.assertEqual(set(self.send(NOW).values()), {'wrong-installation'})
        for item in cluster.exits:
            cluster.stop(item['id'])
            cluster.start(item['id'])
            self.exits.installation[item['id']] = 'new'
        cluster.drain()
        # deploy.sh is done, restore not yet: the panel has no clients.
        self.assertEqual(set(self.send(NOW + snapshot_sender.RECONCILE_SECONDS).values()), {'accepted'})
        cluster.advance(120)
        self.assertEqual(cluster.leaders(), [])
        self.assertFalse(cluster.hy2['a'].running)

    def restore(self, clients, step=2):
        self.panel = clients
        moment = NOW + step * snapshot_sender.RECONCILE_SECONDS
        self.assertEqual(set(self.send(moment).values()), {'accepted'})
        self.cluster.advance(60)

    def test_restored_once_and_only_after_restore(self):
        self.emergency_then_new_entry()
        # The previous profiles do not work before restore: nobody is told.
        self.assertEqual(self.restored(), {})
        # Backup without client 2; client 3 never got an emergency profile.
        self.restore([client(1), client(3, chat=103)])
        self.assertEqual(self.restored(), {101: 1})
        # Agent restarts and later panel edits send nothing more.
        for item in self.cluster.exits:
            self.cluster.stop(item['id'])
            self.cluster.start(item['id'])
        self.cluster.drain()
        self.cluster.advance(60)
        self.restore([client(1), client(2), client(3, chat=103)], step=3)
        self.assertEqual(self.restored(), {101: 1})

    def test_another_person_under_the_same_id_is_not_told(self):
        self.emergency_then_new_entry()
        self.restore([client(1, chat=555), client(2)])
        self.assertEqual(self.restored(), {102: 1})


if __name__ == '__main__':
    unittest.main()
