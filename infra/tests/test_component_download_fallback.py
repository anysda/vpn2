#!/usr/bin/env python3
"""Stage downloads from GitHub releases survive a releases/download outage.

releases/download answers 504 all the time (GitHub incident), the releases API
still serves the asset. Each component a stage takes from GitHub (wgcf,
AdGuardHome, node_exporter, Caddy, sing-box) must arrive through the API,
checked by its sha256; a substituted file is rejected; with both paths down the
stage stops in bounded time and names the component. Development mode is the
CI stand mode: no release manifest, the pinned hash is in the stage itself.

Offline: github.com and api.github.com are one local HTTP server, curl is a
stub that answers like releases/download during the outage.
"""
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest

ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
LIB = ROOT / 'infra/lib'
STAGES = ROOT / 'infra/scripts'

SHIM = r'''
import importlib.util
import os
import sys
import urllib.request

spec = importlib.util.spec_from_file_location('release', os.environ['REAL_RELEASE'])
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
release.DOWNLOAD_PAUSES = (0, 0, 0)
base = os.environ['FAKE_GITHUB']
original = urllib.request.urlopen


def urlopen(request, *args, **kwargs):
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    for prefix, local in (('https://api.github.com', base + '/api'), ('https://github.com', base)):
        if url.startswith(prefix + '/'):
            url = local + url[len(prefix):]
            break
    else:
        if not url.startswith(base + '/'):
            raise OSError('network is disabled in this test: ' + url)
    if isinstance(request, urllib.request.Request):
        request.full_url = url
    else:
        request = url
    return original(request, *args, **kwargs)


urllib.request.urlopen = urlopen
if 'artifact' in sys.argv:
    sys.argv += ['--cache', os.environ['RELEASE_CACHE']]
sys.exit(release.main())
'''


def pinned(path, name):
    return re.search(rf"^\s*(?:readonly\s+)?{name}=['\"]?([^'\"\s]+)", path.read_text(), re.M)[1]


def tar_gz(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, (data, mode) in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeGitHub:
    """releases/download, releases API and asset storage on one port."""

    def __init__(self, assets):
        self.assets = assets  # '/owner/repo/releases/download/tag/name' -> bytes
        self.api = 200
        self.served = {}
        self.hits = {'download': 0, 'api': 0}
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):  # noqa - called by BaseHTTPRequestHandler
                pass

            def reply(self, status, body=b'', headers=()):
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa - called by BaseHTTPRequestHandler
                path = self.path
                if '/releases/download/' in path and not path.startswith('/api/'):
                    owner.hits['download'] += 1
                    self.reply(504)
                    return
                listing = re.fullmatch(r'/api/repos/([^/]+)/([^/]+)/releases/tags/([^/]+)', path)
                if listing:
                    owner.hits['api'] += 1
                    if owner.api != 200:
                        self.reply(owner.api)
                        return
                    prefix = '/{}/{}/releases/download/{}/'.format(*listing.groups())
                    assets = [{'name': key.rsplit('/', 1)[1], 'url': f'{owner.base}/api/assets/{index}'}
                              for index, key in enumerate(owner.assets) if key.startswith(prefix)]
                    self.reply(200, json.dumps({'assets': assets}).encode())
                    return
                asset = re.fullmatch(r'/api/assets/(\d+)', path)
                if asset:
                    owner.hits['api'] += 1
                    if owner.api != 200:
                        self.reply(owner.api)
                        return
                    # GitHub answers an asset with a redirect to its object storage.
                    self.reply(302, headers=[('Location', f'{owner.base}/blob/{asset[1]}')])
                    return
                blob = re.fullmatch(r'/blob/(\d+)', path)
                if blob:
                    key = list(owner.assets)[int(blob[1])]
                    body = owner.served.get(key, owner.assets[key])
                    self.reply(200, body)
                    return
                self.reply(404)

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def components():
    """name -> (releases/download path, payload, build(work, lib) -> (script, installed file))."""
    wgcf_ver = pinned(STAGES / '10-foreign.sh', 'WGCF_VER')
    agh_ver = pinned(STAGES / '22-adguard.sh', 'AGH_VER')
    caddy_ver = pinned(LIB / 'anysda-svc.sh', 'CADDY_VER')
    sb_ver = pinned(LIB / 'install-sing-box.sh', 'SB_VER')
    sb_binary = f'#!/bin/sh\necho "sing-box version {sb_ver}"\n'.encode()

    def wgcf(work, lib):
        source = (STAGES / '10-foreign.sh').read_text()
        block = 'WGCF_VER=' + source.split('WGCF_VER=', 1)[1].split('\ncd /etc/anysda', 1)[0]
        return f'source {shlex.quote(str(lib / "anysda-svc.sh"))}\n' + \
            block.replace('/usr/local/bin/', f'{work}/'), work / 'wgcf'

    def adguard(work, lib):
        source = (STAGES / '22-adguard.sh').read_text()
        version = re.search(r"^AGH_VER=.*$", source, re.M)[0]
        sha = re.search(r"^AGH_SHA256=.*$", source, re.M)
        block = source.split('if [[ "$need_install" -eq 1 ]]; then', 1)[1].split('\n"$AGH_BIN" --version', 1)[0]
        return (f'source {shlex.quote(str(lib / "anysda-svc.sh"))}\n{version}\n{sha[0] if sha else ""}\n'
                f'AGH_BIN={shlex.quote(str(work / "AdGuardHome"))}\nneed_install=1\n'
                'if [[ "$need_install" -eq 1 ]]; then' + block), work / 'AdGuardHome'

    def caddy(work, lib):
        return (f'source {shlex.quote(str(lib / "anysda-svc.sh"))}\n'
                'caddy() { :; }\n'
                f'deb_install() {{ cp "$1" {shlex.quote(str(work / "caddy.deb"))}; }}\n'
                'caddy_install\n'), work / 'caddy.deb'

    def sing_box(work, lib):
        return f'bash {shlex.quote(str(lib / "install-sing-box.sh"))} {shlex.quote(str(work / "sing-box"))}\n', \
            work / 'sing-box'

    return {
        'wgcf': (f'/ViRb3/wgcf/releases/download/v{wgcf_ver}/wgcf_{wgcf_ver}_linux_amd64',
                 b'wgcf executable', wgcf, b'wgcf executable'),
        'adguard': (f'/AdguardTeam/AdGuardHome/releases/download/{agh_ver}/AdGuardHome_linux_amd64.tar.gz',
                    tar_gz({'AdGuardHome/AdGuardHome': (b'adguard executable', 0o755)}), adguard,
                    b'adguard executable'),
        'caddy': (f'/caddyserver/caddy/releases/download/v{caddy_ver}/caddy_{caddy_ver}_linux_amd64.deb',
                  b'caddy package', caddy, b'caddy package'),
        'sing-box': (f'/SagerNet/sing-box/releases/download/v{sb_ver}/sing-box-{sb_ver}-linux-amd64.tar.gz',
                     tar_gz({f'sing-box-{sb_ver}-linux-amd64/sing-box': (sb_binary, 0o755)}), sing_box,
                     sb_binary),
    }


class TestComponentDownloadFallback(unittest.TestCase):
    def setUp(self):
        self.components = components()
        self.server = FakeGitHub({path: payload for path, payload, _build, _want in self.components.values()})
        self.addCleanup(self.server.close)
        self.tmp = Path(tempfile.mkdtemp(prefix='component-fallback-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lib = self.tmp / 'lib'
        self.lib.mkdir()
        (self.lib / 'release.py').write_text(SHIM)
        tools = self.tmp / 'bin'
        tools.mkdir()
        # releases/download during the incident, as curl reports it.
        (tools / 'curl').write_text('#!/bin/sh\necho "curl: (22) The requested URL returned error: 504" >&2\nexit 22\n')
        (tools / 'uname').write_text('#!/bin/sh\necho x86_64\n')
        for tool in tools.iterdir():
            tool.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{tools}:{os.environ["PATH"]}', TMPDIR=str(self.tmp),
                        VPN2_DEVELOPMENT='true', HOST_TAG='test', REAL_RELEASE=str(LIB / 'release.py'),
                        FAKE_GITHUB=self.server.base)
        self.env.pop('VPN2_RELEASE_MANIFEST', None)

    def libraries(self, digest):
        """Library copies next to the shim, the pinned hashes replaced by the fake payload's."""
        for name in ('anysda-svc.sh', 'install-sing-box.sh'):
            text = (LIB / name).read_text()
            text = re.sub(r"((?:CADDY_SHA256|SB_SHA256_AMD64)=')[0-9a-f]{64}", lambda m: m[1] + digest, text)
            (self.lib / name).write_text(text)

    def run_component(self, name):
        path, payload, build, want = self.components[name]
        digest = hashlib.sha256(payload).hexdigest()
        work = Path(tempfile.mkdtemp(dir=self.tmp, prefix=name + '-'))
        self.libraries(digest)
        script, installed = build(work, self.lib)
        script = re.sub(r"((?:WGCF|AGH|NE)_SHA256=')[0-9a-f]{64}", lambda m: m[1] + digest, script)
        env = dict(self.env, RELEASE_CACHE=str(work / 'cache'))
        started = time.monotonic()
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], env=env,
                                capture_output=True, text=True, timeout=120)
        return result, installed, want, time.monotonic() - started

    def test_api_fallback_installs_each_component(self):
        for name in self.components:
            with self.subTest(component=name):
                before = dict(self.server.hits)
                result, installed, want, _elapsed = self.run_component(name)
                self.assertEqual(result.returncode, 0, f'{name} did not survive the outage:\n{result.stderr}')
                self.assertTrue(installed.exists(), f'{name} was not installed')
                self.assertEqual(installed.read_bytes(), want)
                self.assertGreater(self.server.hits['api'], before['api'], f'{name} did not use the releases API')

    def test_substituted_asset_is_rejected(self):
        for name, (path, _payload, _build, _want) in self.components.items():
            with self.subTest(component=name):
                self.server.served = {path: b'substituted ' + name.encode()}
                try:
                    result, installed, _want, _elapsed = self.run_component(name)
                finally:
                    self.server.served = {}
                self.assertNotEqual(result.returncode, 0, f'{name}: a substituted file was accepted')
                self.assertFalse(installed.exists(), f'{name}: a substituted file was installed')

    def test_both_paths_down_name_the_component(self):
        self.server.api = 504
        for name in self.components:
            with self.subTest(component=name):
                before = dict(self.server.hits)
                result, installed, _want, elapsed = self.run_component(name)
                self.assertNotEqual(result.returncode, 0, f'{name}: stage passed without the file')
                self.assertFalse(installed.exists())
                self.assertLess(elapsed, 60, f'{name}: no bounded failure')
                self.assertGreater(self.server.hits['api'], before['api'], f'{name}: the releases API was not tried')
                self.assertIn(name, result.stderr, f'{name}: error does not name the component:\n{result.stderr}')
                self.assertIn('504', result.stderr)


if __name__ == '__main__':
    unittest.main()
