"""A stale upstream socket must not turn a mutation into HTTP 502."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import shlex
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
DOCKER = shlex.split(os.environ.get('DOCKER', 'docker'))


def proxy_blocks(source):
    for match in re.finditer(r'reverse_proxy (?:panel|127\.0\.0\.1):\d+', source):
        start, end = match.span()
        if source[end:].lstrip(' ').startswith('{'):
            end = source.index('{', end) + 1
            depth = 1
            while depth:
                depth += (source[end] == '{') - (source[end] == '}')
                end += 1
        yield source[start:end]


@unittest.skipUnless(os.environ.get('RUN_CADDY_TESTS'), 'requires Docker/Caddy')
class PanelProxyTests(unittest.TestCase):
    def test_post_after_upstream_idle_close(self):
        class Backend(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'  # noqa - read by BaseHTTPRequestHandler

            def do_GET(self):  # noqa - HTTP handler
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')

            def do_POST(self):  # noqa - HTTP handler
                if getattr(self, 'seen', False):
                    # Deterministic close/request race: the pooled connection
                    # expires exactly as the non-idempotent request arrives.
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                               struct.pack('ii', 1, 0))
                    self.close_connection = True  # noqa - read by BaseHTTPRequestHandler
                    return
                self.seen = True
                self.do_GET()

            def log_message(self, *_args):  # noqa - HTTP handler
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        for path in ['app/e2e/support/panel-stand.sh', 'infra/scripts/30-frontend.sh']:
            blocks = list(proxy_blocks((ROOT / path).read_text()))
            self.assertEqual(len(blocks), 1 if 'panel-stand' in path else 2)
            for index, block in enumerate(blocks):
                with self.subTest(path=path, block=index), tempfile.TemporaryDirectory() as tmp:
                    with socket.socket() as sock:
                        sock.bind(('127.0.0.1', 0))
                        port = sock.getsockname()[1]
                    upstream = f'127.0.0.1:{server.server_port}'
                    block = re.sub(r'(panel|127\.0\.0\.1):\d+', upstream, block)
                    config = Path(tmp) / 'Caddyfile'
                    config.write_text(f'{{\n admin off\n}}\n:{port} {{\n{block}\n}}\n')
                    name = f'vpn2-proxy-test-{os.getpid()}'
                    subprocess.run([*DOCKER, 'run', '-d', '--rm', '--name', name,
                                    '--network', 'host', '-v', f'{config}:/etc/caddy/Caddyfile:ro',
                                    'mirror.gcr.io/library/caddy:2'], check=True, capture_output=True)
                    try:
                        deadline = time.monotonic() + 10
                        while True:
                            try:
                                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
                                connection.request('GET', '/')
                                response = connection.getresponse()
                                response.read()
                                connection.close()
                                break
                            except OSError:
                                self.assertLess(time.monotonic(), deadline)
                                time.sleep(.02)
                        statuses = []
                        for _ in range(2):
                            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                            connection.request('POST', '/', body=b'')
                            response = connection.getresponse()
                            statuses.append(response.status)
                            response.read()
                            connection.close()
                        print(f'panel proxy {path} #{index}: POST statuses={statuses}', flush=True)
                        self.assertEqual(statuses, [200, 200])
                    finally:
                        subprocess.run([*DOCKER, 'rm', '-f', name], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
