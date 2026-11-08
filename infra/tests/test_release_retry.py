#!/usr/bin/env python3
"""artifact() survives 503/504 from releases/download: retries, then the releases API."""
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
DATA = b'release archive'
DOWNLOAD = '/o/r/releases/download/v1/file.tgz'


def module(name):
    path = ROOT / 'infra/lib' / (name + '.py')
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Server:
    """Fake github.com and api.github.com on one port; status per route."""

    def __init__(self, download=504, api=200, asset=200, body=DATA):
        self.status = {'download': download, 'api': api, 'asset': asset}
        self.body = body
        self.hits = {'download': 0, 'api': 0, 'asset': 0, 'blob': 0}
        self.accept = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):  # noqa - called by BaseHTTPRequestHandler
                pass

            def reply(self, route, status, body=b'', headers=()):
                owner.hits[route] += 1
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa - called by BaseHTTPRequestHandler
                if self.path == DOWNLOAD:
                    self.reply('download', owner.status['download'], DATA if owner.status['download'] == 200 else b'')
                elif self.path == '/repos/o/r/releases/tags/v1':
                    assets = [{'name': 'other.tgz', 'url': owner.base + '/repos/o/r/releases/assets/1'},
                              {'name': 'file.tgz', 'url': owner.base + '/repos/o/r/releases/assets/2'}]
                    self.reply('api', owner.status['api'], json.dumps({'assets': assets}).encode())
                elif self.path == '/repos/o/r/releases/assets/2':
                    owner.accept.append(self.headers.get('Accept'))
                    if owner.status['asset'] != 200:
                        self.reply('asset', owner.status['asset'])
                    else:  # GitHub answers the asset with a redirect to its object storage.
                        self.reply('asset', 302, headers=[('Location', owner.base + '/blob')])
                elif self.path == '/blob':
                    self.reply('blob', 200, owner.body)
                else:
                    self.send_error(404)

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ArtifactRetryTests(unittest.TestCase):
    def fetch(self, server):
        release = module('release')
        work = Path(tempfile.mkdtemp())
        manifest = {'binaries': {'binary': {'url': server.base + DOWNLOAD,
                                            'sha256': hashlib.sha256(DATA).hexdigest()}}}
        pauses = []
        with patch.object(release, 'GITHUB_DOWNLOAD', server.base, create=True), \
                patch.object(release, 'GITHUB_API', server.base, create=True), \
                patch.object(release.time, 'sleep', side_effect=pauses.append):
            try:
                release.artifact(manifest, 'binary', work / 'cache', work / 'out')
            except release.ReleaseError as exc:
                return work, pauses, exc
        return work, pauses, None

    def test_504_is_retried_then_fetched_through_releases_api(self):
        server = Server(download=504)
        self.addCleanup(server.close)
        work, pauses, error = self.fetch(server)
        self.assertIsNone(error)
        self.assertGreater(server.hits['download'], 1, 'main URL is not retried')
        self.assertEqual(pauses, sorted(pauses), 'pauses do not grow')
        self.assertEqual(len(pauses), server.hits['download'] - 1)
        self.assertEqual(server.accept, ['application/octet-stream'])
        self.assertEqual((work / 'out').read_bytes(), DATA)
        print(f'504: {server.hits["download"]} attempts, pauses {pauses}, then releases API; sha256 ok')

    def test_404_goes_to_api_without_retries(self):
        server = Server(download=404)
        self.addCleanup(server.close)
        work, pauses, error = self.fetch(server)
        self.assertIsNone(error)
        self.assertEqual(server.hits['download'], 1)
        self.assertEqual((work / 'out').read_bytes(), DATA)

    def test_api_path_still_checks_sha256(self):
        server = Server(download=504, body=b'tampered archive')
        self.addCleanup(server.close)
        work, _pauses, error = self.fetch(server)
        self.assertIsNotNone(error)
        self.assertIn('digest mismatch', str(error))
        self.assertFalse((work / 'out').exists())
        self.assertEqual(list((work / 'cache').iterdir()), [])

    def test_failure_everywhere_names_url_and_attempts(self):
        server = Server(download=504, api=504)
        self.addCleanup(server.close)
        work, _pauses, error = self.fetch(server)
        self.assertIsNotNone(error)
        message = str(error)
        self.assertIn(server.base + DOWNLOAD, message)
        self.assertIn(f'{server.hits["download"]} attempts', message)
        self.assertIn(server.base + '/repos/o/r/releases/tags/v1', message)
        self.assertIn('504', message)
        self.assertFalse((work / 'out').exists())
        self.assertEqual(list((work / 'cache').iterdir()), [])
        print(message)


if __name__ == '__main__':
    unittest.main(verbosity=2)
