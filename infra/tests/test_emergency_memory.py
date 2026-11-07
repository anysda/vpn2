"""Emergency agent and snapshot sender keep memory and descriptors flat.

Each loop runs in its own interpreter, so the measurement holds only that
loop. The panel and the SOCKS proxy the entry probes live in this process.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
EMERGENCY = ROOT / 'infra/emergency'
TESTS = Path(__file__).resolve().parent
WARMUP = 100
ROUNDS = 1000
# Allocator and import noise; the leak this guards against was ~230 KiB a round.
RSS_SLACK_KIB = 4096
FD_SLACK = 2

MEASURE = '''
import os
def rss():
    with open('/proc/self/status') as status:
        return next(int(line.split()[1]) for line in status if line.startswith('VmRSS'))
def fds():
    return len(os.listdir('/proc/self/fd'))
'''

ENTRY_LOOP = MEASURE + '''
import json, select, sys, time
sys.path.insert(0, sys.argv[1])
from vpn2_emergency.agent import Agent
from vpn2_emergency.lifecycle import Lifecycle
config, state, warmup, rounds = json.loads(sys.argv[2]), sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
now = 1000.0
agents = {}
for node in ('entry', 'x1'):
    agent = Agent(dict(config, node=node), f'{state}/{node}', clock=lambda: now,
                  snapshot_loader=lambda _directory: None)
    agent.lifecycle = Lifecycle(agent)
    agents[node] = agent
entry = agents['entry']
start = None
for index in range(warmup + rounds):
    if index == warmup:
        start = rss(), fds()
    now += 15
    for agent in agents.values():
        agent.tick(now)
    deadline = time.monotonic() + 10
    while entry.lifecycle.pending and time.monotonic() < deadline:
        time.sleep(0.001)
        entry.lifecycle.collect(now)
    for _ in range(4):
        for agent in agents.values():
            while select.select([agent.sock], [], [], 0)[0]:
                packet, address = agent.sock.recvfrom(65536)
                agent.receive(packet, address, now)
print(json.dumps({'rss': [start[0], rss()], 'fds': [start[1], fds()], 'ready': entry.ready,
                  'reply': agents['x1'].entry_reply is not None}))
'''

SENDER_LOOP = MEASURE + '''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
from vpn2_emergency import snapshot_sender
import test_emergency_snapshot as fixture
root, warmup, rounds = Path(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
panel = fixture.Panel(root)
panel.client(1, 'alice')
exits = fixture.Exits(root / 'exits')
config = {'state_dir': root / 'sender', 'agent': fixture.AGENT}
statuses = set()
start = None
for index in range(warmup + rounds):
    if index == warmup:
        start = rss(), fds()
    now = fixture.NOW + 5 * index
    if index % 50 == 0:
        panel.edit(1, index % 100 == 0)
    exits.now = now
    exits.requests.clear()
    statuses |= set(snapshot_sender.run_once(
        config, exits.deliver,
        lambda moment: snapshot_sender.collect(panel.path, panel.telegram, moment), now=now).values())
print(json.dumps({'rss': [start[0], rss()], 'fds': [start[1], fds()], 'statuses': sorted(statuses)}))
'''


class Panel(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.hits += 1
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'{}')

    def log_message(self, *_args):  # noqa - called by BaseHTTPRequestHandler
        pass


def socks_proxy(listener):
    while True:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        with connection:
            try:
                connection.recv(3)
                connection.sendall(b'\5\0')
                connection.recv(300)
                connection.sendall(b'\5\0\0\1' + bytes(6))
            except OSError:
                pass


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class MemoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def run_loop(self, code, *args):
        result = subprocess.run([sys.executable, '-c', code, *map(str, args)], capture_output=True,
                                text=True, timeout=600, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def assert_flat(self, measured, what):
        (rss_start, rss_end), (fds_start, fds_end) = measured['rss'], measured['fds']
        print(f'{what}: RSS {rss_start} -> {rss_end} KiB, fd {fds_start} -> {fds_end} '
              f'over {ROUNDS} rounds', file=sys.stderr)
        self.assertLessEqual(rss_end - rss_start, RSS_SLACK_KIB,
                             f'{what}: RSS grew {rss_start} -> {rss_end} KiB in {ROUNDS} rounds')
        self.assertLessEqual(fds_end - fds_start, FD_SLACK,
                             f'{what}: descriptors grew {fds_start} -> {fds_end} in {ROUNDS} rounds')

    def test_entry_readiness_and_heartbeat_rounds_stay_flat(self):
        panel = ThreadingHTTPServer(('127.0.0.1', 0), Panel)
        panel.hits = 0
        threading.Thread(target=panel.serve_forever, daemon=True).start()
        self.addCleanup(panel.server_close)
        self.addCleanup(panel.shutdown)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(64)
        self.addCleanup(listener.close)
        threading.Thread(target=socks_proxy, args=(listener,), daemon=True).start()
        # Every VPN unit reads as active, so each round probes the panel and SOCKS.
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        systemctl = bin_dir / 'systemctl'
        systemctl.write_text('#!/bin/sh\nshift\nfor unit in "$@"; do echo active; done\n')
        systemctl.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}')
        config = {
            'entry_id': 'entry', 'installation_id': 'memory-test', 'key': '11' * 32,
            'bind': '127.0.0.1',
            'nodes': {'entry': {'host': '127.0.0.1', 'port': free_udp_port()},
                      'x1': {'host': '127.0.0.1', 'port': free_udp_port()}},
            'recovery': {'units': ['sing-box'],
                         'panel': f'http://127.0.0.1:{panel.server_port}/api/version',
                         'socks': list(listener.getsockname()), 'target': ['example.com', 80]}}
        measured = self.run_loop(ENTRY_LOOP, EMERGENCY, json.dumps(config), self.root / 'state',
                                 WARMUP, ROUNDS)
        # The probe really passed: the panel answered every round, the exit heard the entry.
        self.assertTrue(measured['ready'])
        self.assertTrue(measured['reply'])
        self.assertGreaterEqual(panel.hits, WARMUP + ROUNDS)
        self.assert_flat(measured, 'entry')

    def test_snapshot_sender_rounds_stay_flat(self):
        self.env = dict(os.environ)
        measured = self.run_loop(SENDER_LOOP, EMERGENCY, TESTS, self.root, WARMUP, ROUNDS)
        self.assertEqual(set(measured['statuses']) - {'accepted', 'unchanged'}, set())
        self.assertIn('accepted', measured['statuses'])
        self.assert_flat(measured, 'snapshot sender')


if __name__ == '__main__':
    unittest.main()
