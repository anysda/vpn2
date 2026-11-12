"""The panel opens only over HTTPS with a valid certificate.

Real Caddy runs the Caddyfile rendered by stage 30 and the snippets written by
vpn2 tls. Only the issuer policy (vpn2-tls.conf) is replaced by a stand-in
certificate made with openssl, so the routes decide alone: a self-signed or
expired certificate that Caddy still serves must not open the panel.
"""

import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest

from containers_support import panel_tls
import release_fetch


ROOT = Path(os.environ.get('VPN2_TEST_ROOT', Path(__file__).resolve().parents[2]))
LE = 'https://acme-v02.api.letsencrypt.org/directory'
ISSUER = 'acme-v02.api.letsencrypt.org-directory'
IP = '192.0.2.1'
DOMAIN = 'vpn.example.test'
MARK = 'vpn2-panel-backend'
HOUR = 3600


def caddy_binary():
    """Caddy of the version stage 30 installs (CADDY_VER), downloaded once and checked by SHA-512."""
    version = re.search(r"^CADDY_VER='([^']+)'", (ROOT / 'infra/lib/anysda-svc.sh').read_text(), re.M)[1]
    for candidate in (os.environ.get('CADDY_BIN'), shutil.which('caddy')):
        if candidate:
            out = subprocess.run([candidate, 'version'], capture_output=True, text=True).stdout
            if out.startswith(f'v{version} '):
                return candidate
    cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'vpn2-tests' / f'caddy-{version}'
    binary = cache / 'caddy'
    if binary.exists():
        return str(binary)
    name = f'caddy_{version}_linux_amd64.tar.gz'
    data = release_fetch.fetch('caddyserver/caddy', f'v{version}', name)
    sums = release_fetch.fetch('caddyserver/caddy', f'v{version}', f'caddy_{version}_checksums.txt').decode()
    want = next(line.split()[0] for line in sums.splitlines() if line.endswith(' ' + name))
    if hashlib.sha512(data).hexdigest() != want:
        raise AssertionError('caddy archive SHA-512 mismatch')
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        content = archive.extractfile('caddy').read()
    cache.mkdir(parents=True, exist_ok=True)
    temporary = binary.with_name('caddy.new')
    temporary.write_bytes(content)
    temporary.chmod(0o755)
    temporary.replace(binary)
    return str(binary)


CADDY = caddy_binary()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def openssl_date(seconds):
    return time.strftime('%y%m%d%H%M%SZ', time.gmtime(seconds))


def make_cert(directory, host, start, end):
    """Self-signed leaf with the SAN of the host and the given validity (past dates allowed)."""
    work = Path(tempfile.mkdtemp(dir=directory))
    san = f'IP:{host}' if re.fullmatch(r'[0-9.]+', host) else f'DNS:{host}'
    (work / 'index.txt').write_text('')
    (work / 'serial').write_text('01\n')
    (work / 'ca.cnf').write_text(
        f'[ca]\ndefault_ca = local\n[local]\ndatabase = {work}/index.txt\nnew_certs_dir = {work}\n'
        f'serial = {work}/serial\ndefault_md = sha256\npolicy = any\nunique_subject = no\n'
        'x509_extensions = leaf\n[any]\ncommonName = supplied\n'
        f'[leaf]\nsubjectAltName = {san}\nbasicConstraints = CA:false\n')
    key, csr, crt = work / 'key.pem', work / 'req.csr', work / 'cert.pem'
    subprocess.run(['openssl', 'req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                    '-nodes', '-keyout', key, '-subj', f'/CN={host}', '-out', csr],
                   check=True, capture_output=True)
    subprocess.run(['openssl', 'ca', '-batch', '-notext', '-config', work / 'ca.cnf', '-selfsign',
                    '-keyfile', key, '-in', csr, '-out', crt,
                    '-startdate', openssl_date(start), '-enddate', openssl_date(end)],
                   check=True, capture_output=True)
    return crt, key


class Backend(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa - HTTP handler
        body = MARK.encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # noqa - HTTP handler
        pass


def request(port, host, path, tls=False, sni=None):
    """(status, Location, body); status None when the TLS handshake is refused."""
    sock = socket.create_connection(('127.0.0.1', port), timeout=5)
    try:
        if tls:
            # Like `curl -k`: an expired or self-signed certificate is accepted by the client.
            sock = ssl._create_unverified_context().wrap_socket(sock, server_hostname=sni)
        sock.sendall(f'GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
        response = http.client.HTTPResponse(sock)
        response.begin()
        return response.status, response.getheader('Location', ''), response.read().decode(errors='replace')
    except (ssl.SSLError, ConnectionError) as error:
        return None, '', str(error)
    finally:
        sock.close()


class Entry:
    """Stage 30 TLS part rendered into a temporary root plus vpn2 tls runs against it."""

    def __init__(self, test, domain):
        self.test = test
        self.domain = domain
        self.host = domain or IP
        self.dir = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.storage = self.dir / 'var/lib/caddy/.local/share/caddy'
        stubs = self.dir / 'bin'
        stubs.mkdir()
        for name in ('docker', 'systemctl', 'journalctl'):
            (stubs / name).write_text(f'#!/bin/sh\necho "{name} $*" >> {self.dir}/commands.log\n')
            (stubs / name).chmod(0o755)
        (stubs / 'caddy').write_text(f'#!/bin/sh\nexec {CADDY} "$@"\n')
        (stubs / 'caddy').chmod(0o755)
        self.env = dict(os.environ, PATH=f'{stubs}:{os.environ["PATH"]}', HOME=str(self.dir / 'home'),
                        PYTHONPATH=str(ROOT / 'infra/vpn2/src'),
                        XDG_DATA_HOME=str(self.dir / 'validate-data'),
                        XDG_CONFIG_HOME=str(self.dir / 'validate-config'))
        self.render()

    def render(self):
        result = panel_tls(self.dir, self.domain, self.env, entry=IP)
        self.test.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def tick(self, initial=False):
        """One vpn2-tls.service run (timer) or the --initial run of the installation."""
        command = [sys.executable, '-m', 'vpn2.cli', 'tls',
                   '--config', str(self.dir / 'etc/vpn2/tls/config.json')]
        if initial:
            command += ['--initial', '--wait', '0']
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, env=self.env)
        self.test.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def issue(self, start, end):
        """Caddy storage holds this pair as the CA-issued certificate of the host."""
        crt, key = make_cert(self.dir, self.host, start, end)
        name = self.host.lower()
        target = self.storage / 'certificates' / ISSUER / name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy(crt, target / f'{name}.crt')
        shutil.copy(key, target / f'{name}.key')
        (target / f'{name}.json').write_text(json.dumps({'sans': [self.host], 'issuer_data': {'ca': LE}}))
        return crt, key

    def challenge(self, token, key_authorization):
        """HTTP-01 challenge in progress, as Caddy stores it for a distributed solver."""
        identifier = {'type': 'ip' if self.host == IP else 'dns', 'value': self.host}
        target = self.storage / 'acme' / ISSUER / 'challenge_tokens'
        target.mkdir(parents=True, exist_ok=True)
        (target / f'{self.host}.json').write_text(json.dumps({
            'type': 'http-01', 'url': '', 'status': 'pending', 'token': token,
            'keyAuthorization': key_authorization, 'identifier': identifier}))

    def serve(self, cert):
        """Run real Caddy on the entry files; vpn2-tls.conf serves the given pair."""
        return Proxy(self, cert)


class Proxy:
    def __init__(self, entry, cert):
        self.entry = entry
        self.cert = cert

    def __enter__(self):
        entry = self.entry
        self.backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.http, self.https = free_port(), free_port()
        run = Path(tempfile.mkdtemp(dir=entry.dir))
        source = entry.dir / 'etc/caddy'
        for path in source.iterdir():
            if path.is_file():
                (run / path.name).write_text(path.read_text().replace(str(source), str(run)))
        (run / 'vpn2-tls.conf').write_text(f'tls {self.cert[0]} {self.cert[1]}\n')
        caddyfile = (run / 'Caddyfile').read_text()
        caddyfile = caddyfile.replace('127.0.0.1:51821', f'127.0.0.1:{self.backend.server_port}')
        caddyfile = re.sub(r'^:80 \{', f':{self.http} {{', caddyfile, flags=re.M)
        self.entry.test.assertTrue(caddyfile.startswith('{\n'), caddyfile[:200])
        caddyfile = (f'{{\n    admin off\n    http_port {self.http}\n    https_port {self.https}\n'
                     + caddyfile[2:])
        (run / 'Caddyfile').write_text(caddyfile)
        self.log = (run / 'caddy.log').open('w')
        # The storage of the entry: HTTP-01 challenge tokens are read from it.
        env = dict(entry.env, XDG_DATA_HOME=str(entry.storage.parent), XDG_CONFIG_HOME=str(run))
        self.process = subprocess.Popen([CADDY, 'run', '--config', str(run / 'Caddyfile'), '--adapter', 'caddyfile'],
                                        env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 20
        for port in (self.http, self.https):
            while True:
                if self.process.poll() is not None:
                    raise AssertionError('caddy exited:\n' + (run / 'caddy.log').read_text()[-3000:])
                try:
                    socket.create_connection(('127.0.0.1', port), timeout=1).close()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise AssertionError('caddy did not listen:\n' + (run / 'caddy.log').read_text()[-3000:])
                    time.sleep(.05)
        return self

    def __exit__(self, *_exc):
        self.process.terminate()
        self.process.wait(timeout=20)
        self.log.close()
        self.backend.shutdown()
        self.backend.server_close()

    def get(self, host, path, tls=False, sni=None):
        return request(self.https if tls else self.http, host, path, tls, sni)


PATHS = ('/', '/login', '/api/version', '/api/auth/session')


class PanelHttpsOnlyTests(unittest.TestCase):
    def each_mode(self, check):
        """IP-only entry and entry with a panel domain."""
        for domain in ('', DOMAIN):
            with self.subTest(domain=domain or 'ip'):
                check(Entry(self, domain))

    def assert_http_closed(self, proxy, hosts):
        """HTTP gives only a redirect to https or a refusal, never panel pages or API data."""
        for host in hosts:
            for path in PATHS:
                status, location, body = proxy.get(host, path)
                label = f'http://{host}{path}: {status} {location} {body[:80]!r}'
                self.assertNotIn(MARK, body, label)
                self.assertIsNotNone(status, label)
                self.assertGreaterEqual(status, 300, label)
                if status < 400:
                    self.assertTrue(location.startswith('https://'), label)

    def assert_https_closed(self, proxy, entry):
        for path in PATHS:
            status, _location, body = proxy.get(entry.host, path, tls=True, sni=entry.domain or None)
            label = f'https://{entry.host}{path}: {status} {body[:80]!r}'
            self.assertNotIn(MARK, body, label)
            if status is not None:
                self.assertGreaterEqual(status, 400, label)

    def assert_https_open(self, proxy, entry):
        for path in PATHS:
            status, _location, body = proxy.get(entry.host, path, tls=True, sni=entry.domain or None)
            self.assertEqual((status, body), (200, MARK), f'https://{entry.host}{path}')

    def assert_ip_bypass_closed(self, proxy, entry):
        if not entry.domain:
            return
        for path in PATHS:
            for sni in (None, IP):
                status, _location, body = proxy.get(IP, path, tls=True, sni=sni)
                self.assertNotIn(MARK, body, f'https://{IP}{path} sni={sni}: {status}')

    def test_http_never_serves_panel_before_or_after_certificate(self):
        now = time.time()

        def check(entry):
            stand_in = make_cert(entry.dir, entry.host, now - HOUR, now + 6 * 24 * HOUR)
            with entry.serve(stand_in) as proxy:
                self.assert_http_closed(proxy, [entry.host, IP])
            cert = entry.issue(now - HOUR, now + 6 * 24 * HOUR)
            entry.tick()
            with entry.serve(cert) as proxy:
                self.assert_http_closed(proxy, [entry.host, IP])
                # Positive control: the same proxy opens the panel over HTTPS.
                self.assert_https_open(proxy, entry)
        self.each_mode(check)

    def test_not_issued_certificate_keeps_panel_closed_and_says_why(self):
        now = time.time()

        def check(entry):
            output = entry.tick(initial=True)
            # A self-signed fallback that Caddy might serve must not open the panel.
            self_signed = make_cert(entry.dir, entry.host, now - HOUR, now + 30 * 24 * HOUR)
            with entry.serve(self_signed) as proxy:
                self.assert_http_closed(proxy, [entry.host, IP])
                self.assert_https_closed(proxy, entry)
            # The installation output names the reason.
            self.assertIn('панель закрыта', output.lower(), output)
            # The timer run without a certificate keeps it closed as well.
            entry.tick()
            with entry.serve(self_signed) as proxy:
                self.assert_https_closed(proxy, entry)
        self.each_mode(check)

    def test_expired_certificate_on_running_entry_closes_panel_and_renewal_reopens_it(self):
        now = time.time()

        def check(entry):
            cert = entry.issue(now - 5 * 24 * HOUR, now + 24 * HOUR)
            entry.tick()
            with entry.serve(cert) as proxy:
                self.assert_https_open(proxy, entry)
            # Renewal failed and the certificate expired: Caddy keeps serving it.
            expired = entry.issue(now - 8 * 24 * HOUR, now - HOUR)
            output = entry.tick()
            with entry.serve(expired) as proxy:
                self.assert_https_closed(proxy, entry)
                self.assert_http_closed(proxy, [entry.host, IP])
            # The timer run that closed the panel names the reason in the journal.
            self.assertIn('панель закрыта', output.lower(), output)
            # Renewal succeeded later: the timer opens the panel without reinstalling.
            renewed = entry.issue(now - HOUR, now + 6 * 24 * HOUR)
            entry.tick()
            with entry.serve(renewed) as proxy:
                self.assert_https_open(proxy, entry)
        self.each_mode(check)

    def test_entry_ip_instead_of_domain_does_not_open_panel(self):
        now = time.time()
        entry = Entry(self, DOMAIN)
        stand_in = make_cert(entry.dir, DOMAIN, now - HOUR, now + 6 * 24 * HOUR)
        with entry.serve(stand_in) as proxy:
            self.assert_http_closed(proxy, [IP])
            self.assert_ip_bypass_closed(proxy, entry)
        cert = entry.issue(now - HOUR, now + 6 * 24 * HOUR)
        entry.tick()
        with entry.serve(cert) as proxy:
            self.assert_https_open(proxy, entry)
            self.assert_http_closed(proxy, [IP])
            self.assert_ip_bypass_closed(proxy, entry)

    def test_http01_challenge_is_answered_while_panel_is_closed_and_open(self):
        now = time.time()

        def check(entry):
            entry.challenge('vpn2-token', 'vpn2-token.key-authorization')
            # Closed panel (no certificate yet), then open panel (renewal of a working one).
            stand_in = make_cert(entry.dir, entry.host, now - HOUR, now + 6 * 24 * HOUR)
            with entry.serve(stand_in) as proxy:
                status, _location, body = proxy.get(entry.host, '/.well-known/acme-challenge/vpn2-token')
                self.assertEqual((status, body), (200, 'vpn2-token.key-authorization'), 'closed')
            cert = entry.issue(now - HOUR, now + 6 * 24 * HOUR)
            entry.tick()
            with entry.serve(cert) as proxy:
                status, _location, body = proxy.get(entry.host, '/.well-known/acme-challenge/vpn2-token')
                self.assertEqual((status, body), (200, 'vpn2-token.key-authorization'), 'open')
                self.assert_https_open(proxy, entry)
        self.each_mode(check)


if __name__ == '__main__':
    unittest.main()
