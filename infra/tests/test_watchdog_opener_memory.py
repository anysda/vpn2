"""WARP watchdog and YouTube failover ticks keep memory and descriptors flat.

Each loop runs in its own interpreter against a fake Clash API on localhost,
so the measurement holds only the real tick with its real HTTP client.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
WARMUP = 100
ROUNDS = 1000
# Allocator and import noise; a fresh urllib opener per tick left ~230 KiB.
RSS_SLACK_KIB = 4096
FD_SLACK = 2

MEASURE = '''
import importlib.util, json, os, socket, sys
from pathlib import Path
def rss():
    with open('/proc/self/status') as status:
        return next(int(line.split()[1]) for line in status if line.startswith('VmRSS'))
def fds():
    return len(os.listdir('/proc/self/fd'))
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
script, port, root, warmup, rounds = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
'''

WARP_LOOP = MEASURE + '''
# The watchdog accepts only the local Clash API on 9090; reach the fake one
# without holding the shared port on the machine.
connect = socket.create_connection
def redirect(address, *args, **kwargs):
    host, target = address
    return connect((host, port if target == 9090 else target), *args, **kwargs)
socket.create_connection = redirect
wd = load('warp_watchdog', script)
config = {'experimental': {'clash_api': {'external_controller': '127.0.0.1:9090', 'secret': 'test-only'}}}
state = root / 'warp' / 'state.json'
state.parent.mkdir()
actions = set()
start = None
for index in range(warmup + rounds):
    if index == warmup:
        start = rss(), fds()
    actions.add(wd.tick(config, state))
print(json.dumps({'rss': [start[0], rss()], 'fds': [start[1], fds()], 'actions': sorted(actions)}))
'''

YT_LOOP = MEASURE + '''
yt = load('yt_failover', script)
yt.CONFIG = root / 'config.json'
yt.CONFIG.write_text(json.dumps({'experimental': {'clash_api': {
    'external_controller': f'127.0.0.1:{port}', 'secret': 'test-only'}}}))
yt.BOT_SECRET = root / 'no-bot'
state = root / 'yt' / 'state.json'
start = None
for index in range(warmup + rounds):
    if index == warmup:
        start = rss(), fds()
    yt.tick(state, lambda: (True, 'ok'))
print(json.dumps({'rss': [start[0], rss()], 'fds': [start[1], fds()],
                  'state': json.loads(state.read_text())}))
'''


class ClashApi(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.hits += 1
        body = b'{"now": "youtube-ru"}' if self.path.endswith('/youtube-auto') else b'{"delay": 42}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # noqa - called by BaseHTTPRequestHandler
        pass


class OpenerMemoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.api = ThreadingHTTPServer(('127.0.0.1', 0), ClashApi)
        self.api.hits = 0
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)

    def run_loop(self, code, script):
        args = (ROOT / script, self.api.server_port, self.root, WARMUP, ROUNDS)
        result = subprocess.run([sys.executable, '-c', code, *map(str, args)],
                                capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def assert_flat(self, measured, what):
        (rss_start, rss_end), (fds_start, fds_end) = measured['rss'], measured['fds']
        print(f'{what}: RSS {rss_start} -> {rss_end} KiB, fd {fds_start} -> {fds_end} '
              f'over {ROUNDS} ticks', file=sys.stderr)
        self.assertLessEqual(rss_end - rss_start, RSS_SLACK_KIB,
                             f'{what}: RSS grew {rss_start} -> {rss_end} KiB in {ROUNDS} ticks')
        self.assertLessEqual(fds_end - fds_start, FD_SLACK,
                             f'{what}: descriptors grew {fds_start} -> {fds_end} in {ROUNDS} ticks')

    def test_warp_watchdog_ticks_stay_flat(self):
        measured = self.run_loop(WARP_LOOP, 'infra/lib/warp-watchdog.py')
        # Every tick really probed the API and read it as healthy.
        self.assertEqual(measured['actions'], ['healthy'])
        self.assertGreaterEqual(self.api.hits, WARMUP + ROUNDS)
        self.assert_flat(measured, 'warp-watchdog')

    def test_yt_failover_ticks_stay_flat(self):
        measured = self.run_loop(YT_LOOP, 'infra/lib/yt-failover.py')
        self.assertEqual(measured['state']['good'], 3)
        self.assertEqual(measured['state']['pending'], [])
        self.assertGreaterEqual(self.api.hits, WARMUP + ROUNDS)
        self.assert_flat(measured, 'yt-failover')


if __name__ == '__main__':
    unittest.main()
