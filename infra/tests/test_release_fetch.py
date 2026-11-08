"""CI fetches release assets even when github.com/<repo>/releases/download is down.

A stub server plays both hosts: the download page answers 504, the releases API
lists the asset and serves it through a redirect to another host, as GitHub
does. The file must come through the API, the token must reach the API and
stay off the redirect target, and the checksum must still be enforced.
"""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import release_fetch
except ImportError:
    release_fetch = None

REPO = 'owner/tool'
TAG = 'v1.2.3'
NAME = 'tool-1.2.3-linux-amd64.tar.gz'
BODY = b'archive bytes \x00\x01\x02' * 100
TOKEN = 'ci-token-value'


class Stub(BaseHTTPRequestHandler):
    """Download page, releases API and asset storage on one port; hosts tell them apart."""
    primary_status = 504
    seen = []
    silence = threading.Event()

    def do_GET(self):  # noqa - HTTP handler
        host = self.headers['Host'].split(':')[0]
        port = self.server.server_address[1]
        self.seen.append((host, self.path, self.headers.get('Authorization'), self.headers.get('Accept')))
        if self.path == f'/{REPO}/releases/download/{TAG}/{NAME}':
            if self.primary_status == 200:
                return self.reply(200, BODY)
            if self.primary_status is None:
                self.silence.wait(130)
                return None
            return self.reply(self.primary_status, b'<html>Gateway Timeout</html>')
        if self.path == f'/repos/{REPO}/releases/tags/{TAG}':
            assets = [{'name': 'other.txt', 'url': f'http://127.0.0.1:{port}/repos/{REPO}/releases/assets/1'},
                      {'name': NAME, 'url': f'http://127.0.0.1:{port}/repos/{REPO}/releases/assets/2'}]
            return self.reply(200, json.dumps({'tag_name': TAG, 'assets': assets}).encode())
        if self.path == f'/repos/{REPO}/releases/assets/2':
            if self.headers.get('Accept') != 'application/octet-stream':
                return self.reply(200, b'{"json": "metadata"}')
            self.send_response(302)
            self.send_header('Location', f'http://localhost:{port}/blob/2?signed=1')
            self.send_header('Content-Length', '0')
            return self.end_headers()
        if self.path == '/blob/2?signed=1' and host == 'localhost':
            if self.headers.get('Authorization'):
                return self.reply(400, b'only one auth mechanism allowed')
            return self.reply(200, BODY)
        return self.reply(404, b'not found')

    def reply(self, status, body):
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # noqa - HTTP handler
        pass


class ReleaseFetchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(release_fetch, 'infra/tests/release_fetch.py is missing')
        Stub.primary_status = 504
        Stub.seen = []
        Stub.silence = threading.Event()
        self.addCleanup(Stub.silence.set)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        port = self.server.server_address[1]
        self.env = {'RELEASE_FETCH_WEB': f'http://127.0.0.1:{port}',
                    'RELEASE_FETCH_API': f'http://127.0.0.1:{port}',
                    'GITHUB_TOKEN': TOKEN, 'PATH': '/usr/bin:/bin'}
        self.work = Path(tempfile.mkdtemp())

    def cli(self, *args):
        return subprocess.run([sys.executable, str(Path(release_fetch.__file__)), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_504_on_download_page_falls_back_to_api(self):
        out = self.work / 'a.tar.gz'
        run = self.cli(REPO, TAG, NAME, str(out), '--sha256', hashlib.sha256(BODY).hexdigest())
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(out.read_bytes(), BODY)
        self.assertIn('504', run.stderr)
        api = [s for s in Stub.seen if s[1].startswith('/repos/')]
        self.assertTrue(api, Stub.seen)
        self.assertTrue(all(s[2] == f'Bearer {TOKEN}' for s in api), api)
        blob = [s for s in Stub.seen if s[1].startswith('/blob/')]
        self.assertEqual([s[2] for s in blob], [None], Stub.seen)

    def test_silent_download_page_falls_back_to_api_in_seconds(self):
        Stub.primary_status = None
        out = self.work / 'f.tar.gz'
        start = time.monotonic()
        try:
            run = subprocess.run([sys.executable, str(Path(release_fetch.__file__)), REPO, TAG, NAME, str(out),
                                  '--sha256', hashlib.sha256(BODY).hexdigest()],
                                 env=self.env, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            self.fail('silent releases/download held fetch for 30 s without reaching the API')
        took = time.monotonic() - start
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(out.read_bytes(), BODY)
        self.assertLess(took, 20, run.stderr)
        self.assertTrue([s for s in Stub.seen if s[1].startswith('/repos/')], Stub.seen)

    def test_checksum_mismatch_after_fallback_fails_and_leaves_no_file(self):
        out = self.work / 'b.tar.gz'
        run = self.cli(REPO, TAG, NAME, str(out), '--sha256', '0' * 64)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('sha256', run.stderr)
        self.assertFalse(out.exists())

    def test_checksum_is_required(self):
        run = self.cli(REPO, TAG, NAME, str(self.work / 'c'))
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse((self.work / 'c').exists())

    def test_working_download_page_does_not_touch_api(self):
        Stub.primary_status = 200
        out = self.work / 'd.tar.gz'
        run = self.cli(REPO, TAG, NAME, str(out), '--sha256', hashlib.sha256(BODY).hexdigest())
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(out.read_bytes(), BODY)
        self.assertFalse([s for s in Stub.seen if s[1].startswith('/repos/')], Stub.seen)

    def test_missing_asset_in_release_is_an_error(self):
        run = self.cli(REPO, TAG, 'absent.tar.gz', str(self.work / 'e'), '--sha256', '0' * 64)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('absent.tar.gz', run.stderr)

    def test_library_fetch_falls_back_too(self):
        """test_panel_https_only takes caddy and its checksums through fetch()."""
        env = {k: v for k, v in self.env.items() if k != 'PATH'}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(release_fetch.fetch(REPO, TAG, NAME), BODY)

    def test_caddy_test_module_uses_the_helper(self):
        source = (Path(__file__).resolve().parent / 'test_panel_https_only.py').read_text()
        self.assertNotIn('/releases/download/', source)
        self.assertIn('release_fetch.fetch(', source)

    def test_ci_sing_box_step_uses_the_helper(self):
        workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/checks.yml').read_text()
        self.assertNotIn('/releases/download/', workflow)
        self.assertIn('infra/tests/release_fetch.py SagerNet/sing-box', workflow)


if __name__ == '__main__':
    unittest.main()
