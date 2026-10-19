"""Local emergency HTTPS panel. Event/queue adapters are connected by the agent.

Build assets with `pnpm emergency:build` in app and install the resulting static
directory beside this module. No Node runtime or primary database is used here.
Providers must return data for the requested generation, never cached old keys.
"""

import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import mimetypes
import os
from pathlib import Path
import secrets
import ssl
import string
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import tgqueue

LOG = logging.getLogger(__name__)


def generate_password():
    groups = (string.ascii_uppercase, string.ascii_lowercase, string.digits)
    while True:
        password = ''.join(secrets.choice(''.join(groups)) for _ in range(30))
        if all(any(char in group for char in password) for group in groups):
            return password


def _snapshot(state_dir):
    from . import snapshot  # Installed by the snapshot module's deployment.
    return snapshot.load(state_dir)


def _empty(_generation):
    return {}


ATTEMPTS = 10
BLOCK_SECONDS = 300
TRACKED_ADDRESSES = 1024
DENIED = {'клиент заморожен', 'срок доступа истёк', 'неизвестные или конфликтующие права'}


def delivery_summary(rows, emergency_id, generation):
    """Count tgqueue.delivery_status rows of the current emergency generation only."""
    counts = dict(sent=0, pending=0, errors=0, without_telegram=0, denied=0)
    for row in rows:
        if (row.get('emergency'), row.get('generation')) != (emergency_id, generation):
            continue
        state, reason = row.get('state'), row.get('reason')
        if state == 'cancelled':
            continue
        if state == 'sent':
            counts['sent'] += 1
        elif reason == 'нет канала доставки':
            counts['without_telegram'] += 1
        elif reason in DENIED:
            counts['denied'] += 1
        elif state == 'pending':
            counts['pending'] += 1
        else:
            counts['errors'] += 1
    return counts


def _utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


class Panel:
    """One panel per exit; lifecycle calls revoke credentials synchronously."""

    def __init__(self, state_dir, exit_id, public_ip, *, snapshot_loader=_snapshot,
                 profiles=_empty, connections=_empty, delivery=None, broadcast=None):
        self.directory = Path(state_dir)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.auth_file = self.directory / 'web-auth.json'
        self.exit_id = exit_id
        self.public_ip = str(ipaddress.ip_address(public_ip))
        self.snapshot_loader = snapshot_loader
        self.profiles = profiles
        self.connections = connections
        self.delivery = delivery or (lambda _generation: tgqueue.delivery_status(self.directory))
        self.broadcast = broadcast
        self.lock = threading.RLock()
        self.auth = json.loads(self.auth_file.read_text()) if self.auth_file.exists() else {}
        self.sessions = {}
        self.redirect = None
        deadline = self.auth.get('deadline_wall')
        self.deadline = time.monotonic() + max(0, deadline - time.time()) if deadline is not None else None
        self.cancelled = self.auth.get('cancelled', False)
        self.fingerprint = None

    def _save(self):
        temporary = self.auth_file.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(self.auth, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.auth_file)

    def on_emergency_leader(self, emergency_id, generation, autonomous=False):
        """Return admin-only payload AFTER make_server has bound HTTPS.

        The keys are tgqueue.enqueue_admin arguments: the caller adds
        bundle_version, issued_at and event. Restarting within one generation
        returns the same password, so a repeated enqueue stays a no-op.
        """
        with self.lock:
            if (self.auth.get('emergency_id'), self.auth.get('generation')) != (emergency_id, generation):
                password = generate_password()
                self.auth = dict(emergency_id=emergency_id, generation=generation,
                                 password=password, digest=hashlib.sha256(password.encode()).hexdigest(),
                                 attempts={})
                self.sessions.clear()
                self.deadline = None
                self.cancelled = False
            self.auth['autonomous'] = autonomous
            self._save()
            self.redirect = None
            snapshot = self.snapshot_loader(self.directory) or {}
            name = next((x['name'] for x in snapshot.get('exits', []) if x['id'] == self.exit_id), self.exit_id)
            return dict(emergency_id=emergency_id, generation=generation, exit_name=name,
                        panel_url=self.url, panel_password=self.auth['password'], panel_ready=True,
                        certificate_fingerprint=self.fingerprint)

    @property
    def url(self):
        host = f'[{self.public_ip}]' if ':' in self.public_ip else self.public_ip
        return f'https://{host}'

    def on_step_down(self, new_leader_exit_id=None):
        with self.lock:
            self.auth = {}
            self.sessions.clear()
            self._save()
            self.deadline = None
            self.redirect = None
            snapshot = self.snapshot_loader(self.directory) or {}
            for node in snapshot.get('exits', []):
                if node['id'] == new_leader_exit_id and node['id'] != self.exit_id:
                    ip = str(ipaddress.ip_address(node['ip']))
                    self.redirect = f'https://[{ip}]' if ':' in ip else f'https://{ip}'

    def on_entry_recovered(self, deadline_monotonic, deadline_wall=None):
        # deadline_wall is the agreed time from the notices, so the panel shows
        # exactly the minute the clients were told.
        with self.lock:
            if not self.auth:
                return
            self.deadline = deadline_monotonic
            self.cancelled = False
            if deadline_wall is None:
                deadline_wall = time.time() + max(0, deadline_monotonic - time.monotonic())
            self.auth.update(deadline_wall=deadline_wall, cancelled=False)
            self._save()

    def on_return_cancelled(self):
        with self.lock:
            if not self.auth:
                return
            self.deadline = None
            self.cancelled = True
            self.auth.update(deadline_wall=None, cancelled=True)
            self._save()

    def on_emergency_end(self):
        self.on_step_down()

    def login(self, password, address='local'):
        """Attempts are limited per client address, so a stranger cannot lock the admin out."""
        with self.lock:
            now = time.time()
            if not self.auth:
                return 503, None
            attempts = self.auth.setdefault('attempts', {})
            failures, blocked_until = attempts.get(address, (0, 0))
            if now < blocked_until:
                return 429, None
            digest = hashlib.sha256(password.encode()).hexdigest()
            if not hmac.compare_digest(digest, self.auth['digest']):
                failures += 1
                attempts.pop(address, None)
                attempts[address] = (0, now + BLOCK_SECONDS) if failures >= ATTEMPTS else (failures, 0)
                while len(attempts) > TRACKED_ADDRESSES:
                    attempts.pop(next(iter(attempts)))
                self._save()
                return (429 if failures >= ATTEMPTS else 401), None
            if attempts.pop(address, None):
                self._save()
            self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
            if len(self.sessions) >= 32:
                self.sessions.pop(next(iter(self.sessions)))
            session = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(session.encode()).hexdigest()] = now + 12 * 3600
            return 200, session

    def authenticated(self, cookie):
        try:
            parsed = SimpleCookie(cookie)
            token = parsed['__Host-vpn2-emergency'].value
        except (KeyError, ValueError):
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        return bool(self.auth) and self.sessions.get(digest, 0) > time.time()

    def view(self):
        snapshot = self.snapshot_loader(self.directory) or {}
        generation = self.auth.get('generation')
        connections = self.connections(generation)
        profiles = self.profiles(generation)
        clients = []
        now = datetime.now(timezone.utc)
        for client in snapshot.get('clients', []):
            allowed = client.get('allowed') is True
            try:
                allowed = allowed and (not client.get('expires_at') or _utc(client['expires_at']) > now)
            except (ValueError, TypeError):
                allowed = False
            devices = [dict(id=d['id'], name=d['name'], ready=allowed and d['id'] in profiles)
                       for d in client.get('devices', [])]
            statuses = [connections.get(d['id'], 'unknown') for d in devices]
            status = ('connected' if 'connected' in statuses else 'unknown' if 'unknown' in statuses
                      else 'seen' if 'seen' in statuses else 'never')
            clients.append(dict(id=client['id'], name=client['name'], devices=devices,
                                telegram=bool(client.get('telegram_chat_id')), allowed=allowed, status=status))
        try:
            age = max(0, int((now - _utc(snapshot['created_at'])).total_seconds()))
        except (KeyError, ValueError, TypeError):
            age = None
        remaining = max(0, int(self.deadline - time.monotonic())) if self.deadline is not None else None
        # Explicit allow-list: bot token, chat IDs and arbitrary provider fields stay server-side.
        delivery = delivery_summary(self.delivery(generation), self.auth.get('emergency_id'), generation)
        return dict(clients=clients, snapshot_age=age, exit_id=self.exit_id,
                    exit_name=next((x['name'] for x in snapshot.get('exits', []) if x['id'] == self.exit_id), self.exit_id),
                    generation=generation, autonomous=self.auth.get('autonomous', False),
                    cancelled=self.cancelled, remaining=remaining,
                    deadline=(self.auth.get('deadline_wall') or time.time() + remaining)
                    if remaining is not None else None,
                    bot_ready=bool(snapshot.get('bot_token') and self.broadcast),
                    delivery=delivery)

    def profile(self, device_id):
        if not any(d['id'] == device_id and d['ready'] for c in self.view()['clients'] for d in c['devices']):
            return None
        return self.profiles(self.auth['generation']).get(device_id)


class Certificate:
    """The panel certificate: the common IP one, or a private local fallback
    with a fingerprint for the admin message.

    A renewal during an emergency must reach new connections without a
    restart. The listening socket keeps its first context; its SNI callback,
    which runs for every ClientHello (also one without SNI, as for an IP),
    moves the handshake to the current context. Open connections keep theirs.
    """

    def __init__(self, state_dir, public_ip, cert_dir):
        self.ip = str(ipaddress.ip_address(public_ip))
        self.source = Path(cert_dir)
        self.directory = Path(state_dir) / 'web-tls'
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.attempted = None
        common = self._common()
        if common is None:
            cert, key = self.directory / 'cert.pem', self.directory / 'key.pem'
            if not (key.is_file() and self._usable(cert)):
                subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                                '-nodes', '-days', '30', '-subj', f'/CN={self.ip}',
                                '-addext', f'subjectAltName=IP:{self.ip}',
                                '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
                key.chmod(0o600)
            common = self._context(cert, key), self._fingerprint(cert)
        self.context, self.fingerprint = common
        self.context.sni_callback = self._handshake  # noqa - called by OpenSSL

    def _usable(self, cert):
        return cert.is_file() and all(
            subprocess.run(['openssl', 'x509', '-in', str(cert), *args],
                           capture_output=True, check=False).returncode == 0
            for args in (['-checkend', '60', '-noout'], ['-checkip', self.ip, '-noout']))

    @staticmethod
    def _context(cert, key):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2  # noqa - consumed by OpenSSL
        context.load_cert_chain(cert, key)
        return context

    def _fingerprint(self, cert):
        """None for a certificate a browser trusts for the IP, else its SHA-256."""
        trusted = subprocess.run(['openssl', 'verify', '-purpose', 'sslserver', '-verify_ip', self.ip,
                                  '-untrusted', str(cert), str(cert)], capture_output=True, check=False).returncode == 0
        if trusted:
            return None
        der = ssl.PEM_cert_to_DER_cert(cert.read_text().split('-----END CERTIFICATE-----')[0] + '-----END CERTIFICATE-----')
        return hashlib.sha256(der).hexdigest().upper()

    def _common(self):
        """(context, fingerprint) of the common IP pair, or None while unusable.

        The renewal publishes both files with one symlink switch, and two reads
        of the source may straddle it: one copy of the pair is checked and loaded.
        """
        try:
            pair = (self.source / 'fullchain.pem').read_bytes(), (self.source / 'privkey.pem').read_bytes()
        except OSError:
            return None
        self.attempted = pair
        cert, key = self.directory / 'incoming-cert.pem', self.directory / 'incoming-key.pem'
        try:
            for path, data in ((key, pair[1]), (cert, pair[0])):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    output.write(data)
            if not self._usable(cert):
                return None
            return self._context(cert, key), self._fingerprint(cert)
        except (OSError, ssl.SSLError):
            return None
        finally:
            cert.unlink(missing_ok=True)
            key.unlink(missing_ok=True)

    def _handshake(self, connection, _server_name, _context):
        current = self.context
        if connection.context is not current:
            connection.context = current

    def refresh(self):
        """True when new connections now get a renewed common certificate.

        A pair already tried is not checked again; a pair read across the
        renewal's switch differs from the settled one and is tried once more.
        """
        try:
            pair = (self.source / 'fullchain.pem').read_bytes(), (self.source / 'privkey.pem').read_bytes()
        except OSError:
            return False
        if pair == self.attempted:
            return False
        common = self._common()
        if common is None:
            return False
        self.context, self.fingerprint = common
        return True


def tls_context(state_dir, public_ip, cert_dir):
    """Panel certificate; stage 23 also prepares it in advance through this call."""
    return Certificate(state_dir, public_ip, cert_dir)


def make_server(state_dir, *, exit_id, public_ip, address=('0.0.0.0', 443),
                static_dir=None, cert_dir='/etc/vpn2/tls/ip', **providers):
    panel = Panel(state_dir, exit_id, public_ip, **providers)
    assets = Path(static_dir) if static_dir else Path(__file__).with_name('static')
    certificate = tls_context(state_dir, public_ip, cert_dir)
    context, panel.fingerprint = certificate.context, certificate.fingerprint

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, _format, *_args):  # noqa - BaseHTTPRequestHandler hook
            pass  # No URLs, cookies, passwords or profile material in access logs.

        def send(self, code, body=b'', content_type='application/json; charset=utf-8', **headers):
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
            for key, value in headers.items():
                self.send_header(key.replace('_', '-'), value)
            self.end_headers()
            self.wfile.write(body)

        def static(self, name):
            path = (assets / name).resolve()
            if not path.is_relative_to(assets.resolve()) or not path.is_file():
                return self.send(404)
            kind = mimetypes.guess_type(path)[0] or 'application/octet-stream'
            self.send(200, path.read_bytes(), kind)

        def do_GET(self):  # noqa - BaseHTTPRequestHandler hook
            with panel.lock:
                path = urlsplit(self.path).path
                if path == '/instructions':
                    return self.static('instructions.html')
                if path.startswith('/assets/'):
                    return self.static(path.lstrip('/'))
                if panel.redirect:
                    return self.send(307, Location=panel.redirect + '/')
                if not panel.auth:
                    return self.send(503, dict(error='Аварийная панель не активна'))
                if path in ('/', '/login'):
                    return self.static('index.html')
                if not panel.authenticated(self.headers.get('Cookie', '')):
                    return self.send(401)
                if path == '/api/state':
                    return self.send(200, panel.view())
                parts = path.split('/')
                if len(parts) == 5 and parts[1:3] == ['api', 'profile'] and parts[3].isdigit():
                    profile = panel.profile(int(parts[3]))
                    if not profile:
                        return self.send(404)
                    if parts[4] == 'uri':
                        return self.send(200, dict(uri=profile['uri']))
                    if parts[4] == 'qr':
                        return self.send(200, profile['qr_png'], 'image/png')
                    if parts[4] == 'file':
                        file = Path(profile['file']).resolve()
                        if file.is_relative_to(panel.directory.resolve()) and file.is_file():
                            return self.send(200, file.read_bytes(), 'application/octet-stream',
                                             Content_Disposition=f'attachment; filename="hysteria2-{parts[3]}{file.suffix}"')
                self.send(404)

        def do_POST(self):  # noqa - BaseHTTPRequestHandler hook
            with panel.lock:
                # Exact same-origin JSON protects login, logout and queue writes against CSRF.
                origin = self.headers.get('Origin', '')
                if origin != f'https://{self.headers.get("Host", "")}' or self.headers.get('Content-Type') != 'application/json':
                    return self.send(403)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if length <= 0 or length > 20000:
                        return self.send(413)
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        return self.send(400)
                except (ValueError, UnicodeError):
                    return self.send(400)
                if self.path == '/api/login':
                    if not isinstance(body.get('password'), str):
                        return self.send(400)
                    code, session = panel.login(body['password'], self.client_address[0])
                    cookie = f'__Host-vpn2-emergency={session}; Secure; HttpOnly; SameSite=Strict; Path=/'
                    return self.send(code, dict(ok=code == 200), **({'Set_Cookie': cookie} if session else {}))
                if not panel.authenticated(self.headers.get('Cookie', '')):
                    return self.send(401)
                if self.path == '/api/logout':
                    parsed = SimpleCookie(self.headers['Cookie'])
                    digest = hashlib.sha256(parsed['__Host-vpn2-emergency'].value.encode()).hexdigest()
                    panel.sessions.pop(digest, None)
                    return self.send(200, dict(ok=True), Set_Cookie='__Host-vpn2-emergency=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                if self.path == '/api/broadcast':
                    message = body.get('text')
                    if not isinstance(message, str) or not 0 < len(message.strip()) <= 4096:
                        return self.send(400)
                    if not panel.view()['bot_ready']:
                        return self.send(503, dict(error='Рассылка недоступна'))
                    try:
                        panel.broadcast(message.strip(), panel.auth['generation'])
                    except Exception:
                        return self.send(503, dict(error='Не удалось поставить сообщение в очередь'))
                    return self.send(202, dict(message='Сообщение поставлено в очередь. Результат доставки обновляется автоматически.'))
                self.send(404)

    class Server(ThreadingHTTPServer):
        slots = threading.BoundedSemaphore(16)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

        def handle_error(self, request, client_address):
            failure = sys.exc_info()[1]
            # A client that rejects the certificate, a scanner or a dropped
            # connection is not a panel error: one line, no traceback.
            if isinstance(failure, (ssl.SSLError, ConnectionError, TimeoutError)):
                LOG.info('emergency panel: connection from %s dropped: %s', client_address[0],
                         getattr(failure, 'reason', None) or type(failure).__name__)
                return
            super().handle_error(request, client_address)

    server = Server(address, Handler, bind_and_activate=False)
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    try:
        server.server_bind()
        server.server_activate()
    except BaseException:
        server.server_close()
        raise
    server.panel = panel

    def refresh_tls():
        if not certificate.refresh():
            return False
        panel.fingerprint = certificate.fingerprint
        LOG.info('Emergency panel TLS certificate renewed')
        return True

    server.refresh_tls = refresh_tls
    return server
