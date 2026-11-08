"""The images workflow builds the release manifest past a rate-limited GitHub API.

Every source lookup in build-release.py answers 403 (rate limit) once, then
works: the manifest must still come out, the workflow token must reach
api.github.com only, and a lasting failure must name the address and the code
instead of a bare exception name.
"""

import email.message
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
TOKEN = 'ci-token-value'
API = 'https://api.github.com/repos/'


def builder():
    sys.path.insert(0, str(ROOT / 'infra/lib'))
    spec = importlib.util.spec_from_file_location('build_release', ROOT / 'infra/lib/build-release.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Upstream:
    """Fake urlopen: the first request to each address is rate limited."""

    def __init__(self, failures=1):
        self.failures = failures
        self.seen = {}
        self.headers = {}

    def __call__(self, request, timeout=None):  # noqa: ARG002 - urlopen signature
        url = getattr(request, 'full_url', request)
        auth = request.get_header('Authorization') if isinstance(request, urllib.request.Request) else None
        self.headers[url] = auth
        self.seen[url] = self.seen.get(url, 0) + 1
        if self.seen[url] <= self.failures:
            headers = email.message.Message()
            headers['X-RateLimit-Remaining'] = '0'
            raise urllib.error.HTTPError(url, 403, 'rate limit exceeded', headers, io.BytesIO(b''))
        if url.startswith(API):
            assets = [{'name': name, 'browser_download_url': 'https://example.invalid/' + name,
                       'digest': 'sha256:' + 'b' * 64} for name in ASSETS]
            return io.BytesIO(json.dumps({'assets': assets}).encode())
        return io.BytesIO(b'archive')


def asset_names(module):
    value = module.value
    return {
        'wgcf_{}_linux_amd64'.format(value(ROOT, 'infra/scripts/10-foreign.sh', 'WGCF_VER')),
        'caddy_{}_linux_amd64.deb'.format(value(ROOT, 'infra/lib/anysda-svc.sh', 'CADDY_VER')),
        'node_exporter-{}.linux-amd64.tar.gz'.format(value(ROOT, 'infra/scripts/00-bootstrap.sh', 'NE_VERSION')),
        'AdGuardHome_linux_amd64.tar.gz',
    }


ASSETS = set()


class BuildReleaseFetchTests(unittest.TestCase):
    def setUp(self):
        self.module = builder()
        ASSETS.clear()
        ASSETS.update(asset_names(self.module))

    def specs(self, upstream):
        with mock.patch.object(urllib.request, 'urlopen', upstream), \
                mock.patch('time.sleep'), \
                mock.patch.dict(os.environ, {'GITHUB_TOKEN': TOKEN}):
            return self.module.binary_specs(ROOT)

    def test_rate_limited_api_once_still_builds_manifest(self):
        upstream = Upstream(failures=1)
        specs = self.specs(upstream)
        self.assertEqual(specs['caddy']['sha256'], 'b' * 64)
        api = [url for url in upstream.seen if url.startswith(API)]
        self.assertEqual(len(api), 4, upstream.seen)
        for url, auth in upstream.headers.items():
            self.assertEqual(auth, f'Bearer {TOKEN}' if url.startswith(API) else None, url)
        print('build-release: 403 rate limit retried, token on api.github.com only')

    def test_lasting_failure_names_address_and_code(self):
        with self.assertRaises(self.module.ReleaseError) as caught:
            self.specs(Upstream(failures=99))
        self.assertIn(API, str(caught.exception))
        self.assertIn('HTTP 403', str(caught.exception))
        print('build-release: lasting failure:', caught.exception)


if __name__ == '__main__':
    unittest.main()
