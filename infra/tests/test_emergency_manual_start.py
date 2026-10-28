"""VPN2-565: manual emergency start from the admin's Telegram message.

Exits that noticed a silent entry send the admin ONE message with a countdown
and a URL button to the HTTPS port of an exit; opening the link with a valid
token starts the emergency at once through the normal election. Real agents
over UDP, real lifecycle, Telegram queue and panel server; only the Bot API
and Hysteria2 are local fakes. Every exit has its own loopback address, so the
button's host names the exit that serves the link.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import functools
import http.client
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from test_emergency_agent import Clock, Cluster, ROOT
from test_emergency_lifecycle import FakeHy2

sys.path.insert(0, str(ROOT / 'infra/emergency'))
from vpn2_emergency import lifecycle, web  # noqa: E402
from vpn2_emergency.agent import Agent  # noqa: E402

ADMIN = 900
SILENT = 'Entry недоступен'
BACK = 'Entry снова доступен'
BUTTON = 'Запустить сейчас'


class Bot(ThreadingHTTPServer):
    """Bot API: sendMessage answers with a message_id, editMessageText can fail."""

    def __init__(self):
        super().__init__(('127.0.0.1', 0), BotHandler)
        self.lock = threading.Lock()
        self.calls = []
        self.next_id = 100
        self.fail_edits = False

    def sent(self, chat):
        return [call for call in self.calls
                if call['method'] == 'sendMessage' and call['ok'] and call['data']['chat_id'] == chat]

    def edits(self, message_id, ok=True):
        return [call for call in self.calls if call['method'] == 'editMessageText'
                and call['data'].get('message_id') == message_id and call['ok'] is ok]

    def text(self, call):
        """The message as the admin sees it now: the last delivered edit wins."""
        edits = self.edits(call['message_id'])
        return edits[-1]['data']['text'] if edits else call['data']['text']


class BotHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):  # noqa - HTTP handler
        pass

    def reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa - HTTP handler (getMe)
        self.reply(200, {'ok': True, 'result': {'id': 1, 'is_bot': True}})

    def do_POST(self):  # noqa - HTTP handler
        bot = self.server
        method = self.path.rsplit('/', 1)[-1]
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])) or b'{}')
        with bot.lock:
            ok = not (method == 'editMessageText' and bot.fail_edits)
            call = dict(method=method, data=data, ok=ok, message_id=None)
            if ok and method == 'sendMessage':
                bot.next_id += 1
                call['message_id'] = bot.next_id
            bot.calls.append(call)
        if not ok:
            return self.reply(502, {'ok': False, 'error_code': 502, 'description': 'Bad Gateway'})
        result = {'message_id': call['message_id'] or data.get('message_id'),
                  'chat': {'id': data.get('chat_id')}, 'text': data.get('text', '')}
        self.reply(200, {'ok': True, 'result': result})


SNAPSHOTS = {}


def load_snapshot(directory):
    path = Path(directory).resolve()
    for root, snapshot in SNAPSHOTS.items():
        if path.is_relative_to(root):
            return snapshot
    return None


class Exits(Cluster):
    """Entry and three exits of one installation, each exit with its lifecycle."""

    def __init__(self, directory, bot, *, net=1, installation='fixture', key='ab' * 32, chat=ADMIN):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.bot = bot
        self.clock = Clock()
        self.offset = 0
        self.wall = lambda: 1_800_000_000 + self.offset + self.clock.now
        self.blocked = set()
        self.agents = {}
        self.events = {}
        self.configs = {}
        hosts = {'entry': f'127.0.{net}.1', 'a': f'127.0.{net}.2', 'b': f'127.0.{net}.3', 'c': f'127.0.{net}.4'}
        self.exits = [{'id': node, 'priority': i, 'name': f'exit-{node}', 'ip': hosts[node]}
                      for i, node in enumerate('abc')]
        self.full = {'schema': 1, 'installation_id': installation, 'bot_token': '123:fixture-only',
                     'admin_chat_id': chat, 'exits': self.exits, 'created_at': '2026-01-01T00:00:00Z',
                     'clients': [{'id': 1, 'name': 'first', 'telegram_chat_id': chat + 1000, 'allowed': True,
                                  'expires_at': None, 'devices': [{'id': 11, 'name': 'phone'}]}]}
        SNAPSHOTS[self.directory.resolve()] = self.full
        nodes = {}
        for node, host in hosts.items():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.bind((host, 0))
                nodes[node] = {'host': host, 'port': sock.getsockname()[1]}
        for node, host in hosts.items():
            self.configs[node] = dict(node=node, installation_id=installation, key=key, bind=host, nodes=nodes)
            self.start(node)
        self.drain()

    def snapshot(self, _directory):
        return self.full

    def start(self, node):
        agent = Agent(self.configs[node], self.directory / node, clock=self.clock,
                      snapshot_loader=self.snapshot, wall=self.wall)
        original_send = agent.send

        def send(peer, message, *address):
            if (node, peer) not in self.blocked:
                original_send(peer, message, *address)

        agent.send = send
        self.agents[node] = agent
        agent.tick(self.clock())
        if node == agent.entry:
            agent.ready = True
            return
        host = self.configs[node]['nodes'][node]['host']
        make_server = functools.partial(web.make_server, cert_dir=self.directory / 'no-tls')
        agent.lifecycle = lifecycle.Lifecycle(
            agent, hy2_module=FakeHy2(), web_module=types.SimpleNamespace(make_server=make_server),
            threaded=False, panel_address=(host, 0), api_base=f'http://127.0.0.1:{self.bot.server_port}',
            probe_ready=lambda: True, probe_entry=lambda _host: 'entry' in self.agents)
        agent.leader.events = agent.lifecycle

    def close(self):
        for agent in self.agents.values():
            if agent.lifecycle:
                agent.lifecycle.close()
        super().close()

    def exit_agents(self):
        return {node: agent for node, agent in self.agents.items() if node != 'entry'}

    def active(self):
        return sorted(node for node, agent in self.exit_agents().items() if agent.leader.data['active'])

    def starts(self):
        return sum(len(agent.lifecycle.hy2.starts) for agent in self.exit_agents().values())


def token_part(url):
    """Where the one-time token sits: the longest path segment or query value."""
    parts = urlsplit(url)
    places = [('path', i, value) for i, value in enumerate(parts.path.split('/')) if value]
    places += [('query', key, value) for key, value in parse_qsl(parts.query)]
    return max(places, key=lambda place: len(place[2]))


def with_token(url, token):
    """The same link with the token replaced; None drops it."""
    parts = urlsplit(url)
    where, key, _value = token_part(url)
    if where == 'path':
        segments = parts.path.split('/')
        if token is None:
            del segments[key]
        else:
            segments[key] = token
        return urlunsplit(parts._replace(path='/'.join(segments)))
    query = [(name, value) for name, value in parse_qsl(parts.query) if name != key]
    if token is not None:
        query.append((key, token))
    return urlunsplit(parts._replace(query=urlencode(query)))


class ManualStartTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='emergency-manual-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bot = Bot()
        thread = threading.Thread(target=self.bot.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        self.addCleanup(self.bot.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.bot.shutdown)
        module = types.ModuleType('vpn2_emergency.snapshot')
        module.load = load_snapshot
        # The queue and the panel import it lazily: a real module already
        # imported is a package attribute and wins over sys.modules.
        for stub in (patch.dict(sys.modules, {'vpn2_emergency.snapshot': module}),
                     patch.object(sys.modules['vpn2_emergency'], 'snapshot', module, create=True)):
            stub.start()
            self.addCleanup(stub.stop)
        self.addCleanup(SNAPSHOTS.clear)

    def exits(self, **kwargs):
        name = kwargs.get('installation', 'fixture')
        cluster = Exits(self.root / name, self.bot, **kwargs)
        self.addCleanup(cluster.close)
        return cluster

    def silent(self, cluster, seconds=180):
        cluster.stop('entry')
        cluster.advance(seconds)

    def admin_texts(self, chat=ADMIN):
        return [self.bot.text(call) for call in self.bot.sent(chat)]

    def outage_message(self, chat=ADMIN):
        """The one admin message about the silent entry, with its start link."""
        calls = [call for call in self.bot.sent(chat) if SILENT in call['data']['text']]
        self.assertEqual(len(calls), 1, f'сообщений «{SILENT}» в admin_chat: {len(calls)}; '
                                        f'все сообщения админу: {self.admin_texts(chat)}')
        call = calls[0]
        markup = call['data'].get('reply_markup')
        if isinstance(markup, str):
            markup = json.loads(markup)
        buttons = [button for row in (markup or {}).get('inline_keyboard', []) for button in row
                   if button.get('text') == BUTTON and button.get('url')]
        self.assertEqual(len(buttons), 1, f'нет URL-кнопки «{BUTTON}»: {markup}')
        self.assertTrue(buttons[0]['url'].startswith('https://'), buttons[0]['url'])
        return call, buttons[0]['url']

    def open(self, cluster, url, host=None):
        """GET the link on the exit its host names; None if nothing listens there."""
        parts = urlsplit(url)
        host = host or parts.hostname
        nodes = [node for node, agent in cluster.exit_agents().items() if agent.addresses[node][0] == host]
        self.assertEqual(len(nodes), 1, f'ссылка ведёт не на экзит установки: {url}')
        server = cluster.agents[nodes[0]].lifecycle.server
        if server is None:
            return None
        target = urlunsplit(('', '', parts.path or '/', parts.query, ''))
        connection = http.client.HTTPSConnection(
            host, server.server_address[1], timeout=5,
            context=ssl._create_unverified_context())  # noqa: S323 - local self-signed panel
        try:
            connection.request('GET', target, headers={'Host': host})
            response = connection.getresponse()
            response.read()
            return response.status
        except ConnectionError:
            return None
        finally:
            connection.close()

    def assert_refused(self, cluster, status):
        self.assertTrue(status is None or status >= 400, f'ссылка принята: HTTP {status}')
        cluster.advance(60)
        self.assertEqual(cluster.active(), [])
        self.assertEqual(cluster.leaders(), [])
        self.assertEqual(cluster.starts(), 0)

    def started_by_link(self, cluster):
        self.silent(cluster)
        _call, url = self.outage_message()
        silence = 180
        status = self.open(cluster, url)
        self.assertIsNotNone(status, 'по ссылке никто не отвечает')
        self.assertTrue(200 <= status < 300, f'действующий токен отклонён: HTTP {status}')
        for _ in range(6):
            cluster.advance(15)
            silence += 15
            self.assertLessEqual(len(cluster.leaders()), 1, cluster.leaders())
        return url, silence

    # 1
    def test_silent_entry_sends_one_admin_message_with_countdown_and_button(self):
        cluster = self.exits()
        self.silent(cluster)
        call, url = self.outage_message()
        self.assertRegex(call['data']['text'], r'\d+\s*мин', 'нет отсчёта до аварии')
        self.assertIn(urlsplit(url).hostname, [item['ip'] for item in cluster.exits])
        # Until the automatic start nothing else goes out, to nobody else.
        cluster.advance(930)
        self.assertEqual(cluster.leaders(), [])
        self.assertEqual(len(self.bot.sent(ADMIN)), 1, self.admin_texts())
        self.assertEqual([call['data']['chat_id'] for call in self.bot.calls
                          if call['method'] == 'sendMessage'], [ADMIN])

    # 2
    def test_link_starts_emergency_at_once_through_one_leader(self):
        cluster = self.exits()
        _url, silence = self.started_by_link(cluster)
        self.assertLess(silence, 1200)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertEqual(cluster.active(), ['a', 'b', 'c'])
        for node in 'bc':
            self.assertEqual(cluster.agents[node].leader.data['owner'], 'a', node)
        self.assertEqual(cluster.starts(), 1)

    # 3
    def test_repeated_link_starts_nothing_twice(self):
        cluster = self.exits()
        url, _silence = self.started_by_link(cluster)
        self.assertEqual(cluster.leaders(), ['a'])
        generation = cluster.agents['a'].leader.data['generation']
        emergency = cluster.agents['a'].leader.data['emergency_id']
        self.open(cluster, url)
        cluster.advance(90)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertEqual(cluster.starts(), 1)
        self.assertEqual(cluster.agents['a'].leader.data['generation'], generation)
        self.assertEqual(cluster.agents['a'].leader.data['emergency_id'], emergency)

    # 4
    def test_forged_token_starts_nothing(self):
        cluster = self.exits()
        self.silent(cluster)
        _call, url = self.outage_message()
        token = token_part(url)[2]
        middle = len(token) // 2
        forged = token[:middle] + ('A' if token[middle] != 'A' else 'B') + token[middle + 1:]
        self.assert_refused(cluster, self.open(cluster, with_token(url, forged)))

    def test_expired_token_starts_nothing(self):
        cluster = self.exits()
        self.silent(cluster)
        _call, url = self.outage_message()
        # The entry is still silent; only the token's lifetime has passed.
        cluster.offset += 86400
        self.assert_refused(cluster, self.open(cluster, url))

    def test_token_of_another_installation_starts_nothing(self):
        cluster = self.exits()
        other = self.exits(net=2, installation='other', key='cd' * 32, chat=ADMIN + 1)
        self.silent(cluster)
        self.silent(other)
        _call, ours = self.outage_message()
        _call, foreign = self.outage_message(ADMIN + 1)
        self.assert_refused(cluster, self.open(cluster, foreign, host=urlsplit(ours).hostname))
        self.assertEqual(other.active(), [])

    def test_link_without_token_starts_nothing(self):
        cluster = self.exits()
        self.silent(cluster)
        _call, url = self.outage_message()
        self.assert_refused(cluster, self.open(cluster, with_token(url, None)))

    # 5
    def test_entry_back_corrects_message_and_voids_link(self):
        cluster = self.exits()
        self.silent(cluster)
        call, url = self.outage_message()
        cluster.start('entry')
        cluster.drain()
        cluster.advance(60)
        self.assertIn(BACK, self.bot.text(call), 'сообщение не исправлено после возврата entry')
        self.assertEqual(len(self.bot.sent(ADMIN)), 1, self.admin_texts())
        self.assert_refused(cluster, self.open(cluster, url))

    # 6
    def test_link_after_automatic_start_changes_nothing(self):
        cluster = self.exits()
        self.silent(cluster, 1320)
        self.assertEqual(cluster.leaders(), ['a'])
        _call, url = self.outage_message()
        leader = cluster.agents['a'].leader.data
        before = (leader['generation'], leader['emergency_id'], cluster.starts())
        self.open(cluster, url)
        cluster.advance(60)
        self.assertEqual(cluster.leaders(), ['a'])
        self.assertEqual((leader['generation'], leader['emergency_id'], cluster.starts()), before)

    # 7
    def test_exit_that_still_hears_entry_does_not_activate_on_manual_start(self):
        cluster = self.exits()
        # 'b' alone still hears the entry and is cut off from the other exits.
        for node in 'ac':
            cluster.blocked.update({('entry', node), (node, 'entry')})
        for node in 'ac':
            cluster.blocked.update({('b', node), (node, 'b')})
        cluster.advance(180)
        _call, url = self.outage_message()
        status = self.open(cluster, url)
        self.assertTrue(status is not None and 200 <= status < 300, f'HTTP {status}')
        cluster.advance(90)
        self.assertEqual(cluster.leaders(), ['a'])
        # The exits meet again; 'b' hears the entry the whole time.
        cluster.blocked -= {('b', node) for node in 'ac'} | {(node, 'b') for node in 'ac'}
        for _ in range(8):
            cluster.advance(15)
            self.assertNotIn('b', cluster.leaders())
            self.assertFalse(cluster.agents['b'].leader.data['active'])
        self.assertEqual(cluster.leaders(), ['a'])

    # 8
    def test_failed_edit_keeps_message_and_retries(self):
        cluster = self.exits()
        self.silent(cluster)
        call, _url = self.outage_message()
        self.bot.fail_edits = True
        cluster.start('entry')
        cluster.drain()
        cluster.advance(60)
        self.assertTrue(self.bot.edits(call['message_id'], ok=False), 'исправление не пытались отправить')
        self.bot.fail_edits = False
        for _ in range(20):
            if BACK in self.bot.text(call):
                break
            cluster.advance(15)
        self.assertIn(BACK, self.bot.text(call), 'после ошибки editMessageText повтора не было')
        self.assertEqual(len(self.bot.sent(ADMIN)), 1, self.admin_texts())


if __name__ == '__main__':
    unittest.main()
