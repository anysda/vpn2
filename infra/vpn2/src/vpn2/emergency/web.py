"""Аварийная HTTPS-панель экзита. Данные выдачи и очереди подаёт агент.

Статику собирает `pnpm emergency:build`, на узел её кладёт образ emergency.
"""

import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import mimetypes
from pathlib import Path
import secrets
import ssl
import string
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .. import fsutil
from . import hy2, spool

LOG = logging.getLogger(__name__)
STATIC_DIR = '/opt/vpn2-emergency/static'
ATTEMPTS = 10
BLOCK_SECONDS = 300
TRACKED_ADDRESSES = 1024
COOKIE = '__Host-vpn2-emergency'
DENIED = {'клиент заморожен', 'срок доступа истёк', 'неизвестные или конфликтующие права'}
START_PAGES = {'starting': (202, 'Аварийный режим запускается. Адрес и пароль панели придут '
                                'отдельным сообщением.'),
               'invalid': (403, 'Ссылка недействительна.'),
               'gone': (410, 'Ссылка больше не действует: она уже использована, устарела, '
                             'entry снова доступен или аварийный режим уже включён.'),
               'entry_back': (410, 'Entry снова доступен, аварийный режим не нужен.')}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")


def generate_password():
    groups = (string.ascii_uppercase, string.ascii_lowercase, string.digits)
    while True:
        password = ''.join(secrets.choice(''.join(groups)) for _ in range(30))
        if all(any(char in group for char in password) for group in groups):
            return password


def _delivery_kind(row, emergency_id, generation):
    """Счётчик сводки для строки очереди текущего поколения; None - не считается."""
    state, reason = row.get('state'), row.get('reason')
    if (row.get('emergency'), row.get('generation')) != (emergency_id, generation) or state == 'cancelled':
        return None
    if state == 'sent':
        return 'sent'
    if reason == 'нет канала доставки':
        return 'without_telegram'
    return 'denied' if reason in DENIED else 'pending' if state == 'pending' else 'errors'


def delivery_reason(state, reason):
    """Текст причины недоставки из строки очереди."""
    code, _, detail = reason.removeprefix('Telegram ').partition(': ')
    if reason.startswith('Telegram ') and code.isdigit():
        if state == 'failed':
            detail = detail or {'400': 'Telegram отклонил сообщение',
                                '403': 'Telegram запретил отправку'}.get(code, 'ошибка Telegram')
            return f'Не доставлено: {detail} (Telegram {code}), повтора не будет'
        if code == '429':
            return 'Telegram ограничил частоту отправки, повтор автоматически'
        return f'Telegram ответил ошибкой {code}, повтор автоматически'
    if state == 'retry':
        return ('Telegram недоступен, повтор автоматически' if reason.startswith('Telegram недоступен')
                else f'{reason}, повтор автоматически')
    return f'Не отправлено: {reason}'


def _utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def _https(ip):
    return f'https://[{ip}]' if ':' in ip else f'https://{ip}'


class Panel:
    """Панель экзита; смена лидерства отзывает пароль и сеансы сразу."""

    def __init__(self, state_dir, exit_id, public_ip, *, snapshot_loader, profiles, connections,
                 broadcast=None, start=None, delivery=None):
        self.directory = Path(state_dir)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # start(token) -> ключ START_PAGES: ссылка админа «Запустить сейчас»;
        # с link_only до аварии сервер отвечает только на неё.
        self.start = start
        self.link_only = False
        self.auth_file = self.directory / 'web-auth.json'
        self.exit_id = exit_id
        self.public_ip = str(ipaddress.ip_address(public_ip))
        self.snapshot_loader = snapshot_loader
        self.profiles = profiles
        self.connections = connections
        self.delivery = delivery or (lambda _generation: spool.rows(self.directory, audience='client'))
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
        fsutil.atomic(self.auth_file, json.dumps(self.auth))

    def _exit_name(self, snapshot):
        return next((x['name'] for x in snapshot.get('exits', []) if x['id'] == self.exit_id), self.exit_id)

    @property
    def url(self):
        return _https(self.public_ip)

    def on_emergency_leader(self, emergency_id, generation, autonomous=False, event='initial'):
        """Данные для админа после того, как HTTPS слушает; в одном поколении пароль тот же.

        О смене лидера панель говорит только по event 'leader_changed', а не по поколению.
        """
        with self.lock:
            self.link_only = False
            if (self.auth.get('emergency_id'), self.auth.get('generation')) != (emergency_id, generation):
                password = generate_password()
                self.auth = dict(emergency_id=emergency_id, generation=generation, password=password,
                                 digest=hashlib.sha256(password.encode()).hexdigest(), attempts={})
                self.sessions.clear()
                self.deadline = None
                self.cancelled = False
            self.auth.update(autonomous=autonomous, event=event)
            self._save()
            self.redirect = None
            return dict(emergency_id=emergency_id, generation=generation,
                        exit_name=self._exit_name(self.snapshot_loader(self.directory) or {}),
                        panel_url=self.url, panel_password=self.auth['password'], panel_ready=True,
                        certificate_fingerprint=self.fingerprint)

    def on_step_down(self, new_leader_exit_id=None):
        with self.lock:
            self.link_only = False
            self.auth = {}
            self.sessions.clear()
            self._save()
            self.deadline = None
            self.redirect = None
            for node in (self.snapshot_loader(self.directory) or {}).get('exits', []):
                if node['id'] == new_leader_exit_id and node['id'] != self.exit_id:
                    self.redirect = _https(str(ipaddress.ip_address(node['ip'])))

    def on_entry_recovered(self, deadline_monotonic, deadline_wall):
        """deadline_wall - время из сообщений: панель показывает ту же минуту."""
        with self.lock:
            if self.auth:
                self.deadline, self.cancelled = deadline_monotonic, False
                self.auth.update(deadline_wall=deadline_wall, cancelled=False)
                self._save()

    def on_return_cancelled(self):
        with self.lock:
            if self.auth:
                self.deadline, self.cancelled = None, True
                self.auth.update(deadline_wall=None, cancelled=True)
                self._save()

    def login(self, password, address='local'):
        """Попытки считаются по адресу клиента: чужой не запрёт админа."""
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

    def session(self, cookie):
        """Ключ сеанса из cookie; None - сеанса нет."""
        try:
            digest = hashlib.sha256(SimpleCookie(cookie)[COOKIE].value.encode()).hexdigest()
        except (KeyError, ValueError):
            return None
        return digest if self.auth and self.sessions.get(digest, 0) > time.time() else None

    def view(self):
        snapshot = self.snapshot_loader(self.directory) or {}
        emergency, generation = self.auth.get('emergency_id'), self.auth.get('generation')
        connections, profiles = self.connections(generation), self.profiles(generation)
        delivery = dict(sent=0, pending=0, errors=0, without_telegram=0, denied=0)
        errors = {}
        for row in self.delivery(generation):
            kind = _delivery_kind(row, emergency, generation)
            if kind:
                delivery[kind] += 1
            if kind == 'errors':
                errors[row.get('client')] = delivery_reason(row.get('state'), row.get('reason') or '')
        now = datetime.now(timezone.utc)
        clients = []
        for client in snapshot.get('clients', []):
            try:
                expired = bool(client.get('expires_at')) and _utc(client['expires_at']) <= now
                allowed = client.get('allowed') is True and not expired
            except (ValueError, TypeError):
                expired, allowed = False, False
            devices = [dict(id=d['id'], name=d['name'], ready=allowed and d['id'] in profiles)
                       for d in client.get('devices', [])]
            statuses = [connections.get(d['id'], 'unknown') for d in devices]
            status = ('connected' if 'connected' in statuses else 'unknown' if 'unknown' in statuses
                      else 'seen' if 'seen' in statuses else 'never')
            if not allowed:
                status = 'expired' if expired else 'blocked'
            clients.append(dict(id=client['id'], name=client['name'], devices=devices,
                                telegram=bool(client.get('telegram_chat_id')), allowed=allowed, status=status,
                                delivery_error=errors.get(client['id'])))
        try:
            age = max(0, int((now - _utc(snapshot['created_at'])).total_seconds()))
        except (KeyError, ValueError, TypeError):
            age = None
        remaining = max(0, int(self.deadline - time.monotonic())) if self.deadline is not None else None
        # Только перечисленные поля: токен бота и chat id остаются на сервере.
        return dict(clients=clients, snapshot_age=age, exit_id=self.exit_id, exit_name=self._exit_name(snapshot),
                    generation=generation, leader_changed=self.auth.get('event') == 'leader_changed',
                    autonomous=self.auth.get('autonomous', False), cancelled=self.cancelled, remaining=remaining,
                    deadline=(self.auth.get('deadline_wall') or time.time() + remaining)
                    if remaining is not None else None,
                    bot_ready=bool(snapshot.get('bot_token') and self.broadcast), delivery=delivery,
                    delivery_errors=[dict(id=c['id'], name=c['name'], reason=c['delivery_error'])
                                     for c in clients if c['delivery_error']])

    def profile(self, device_id):
        if not any(d['id'] == device_id and d['ready'] for c in self.view()['clients'] for d in c['devices']):
            return None
        return self.profiles(self.auth['generation']).get(device_id)


class Tls:
    """Пара Hysteria2 (hy2.tls_pair) для панели; продлённая доходит до новых соединений без перезапуска.

    Слушающий сокет держит первый контекст; его SNI-обработчик, который зовётся
    на каждый ClientHello (и без SNI, как для IP), переводит рукопожатие на текущий.
    """

    def __init__(self, state_dir, public_ip, cert_dir):
        self.ip = str(ipaddress.ip_address(public_ip))
        self.directory = hy2.tls_pair(state_dir, self.ip, cert_dir)
        self.loaded, self.fingerprint = None, None
        if not self.reload():
            raise OSError('emergency panel: no usable TLS pair')
        self.context.sni_callback = self._handshake

    def reload(self):
        """True, когда новые соединения получат сменившуюся пару."""
        cert = self.directory / 'fullchain.pem'
        try:
            stamp = cert.stat().st_mtime_ns
            if stamp == self.loaded:
                return False
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, self.directory / 'privkey.pem')
        except OSError:
            return False
        self.context, self.loaded = context, stamp
        fingerprint = hy2.fingerprint(self.directory, self.ip)
        self.fingerprint = fingerprint and fingerprint.upper()
        return True

    def _handshake(self, connection, _server_name, _context):
        if connection.context is not self.context:
            connection.context = self.context


def make_server(state_dir, *, exit_id, public_ip, address=('0.0.0.0', 443),
                static_dir=STATIC_DIR, cert_dir=hy2.TLS_SOURCE, **providers):
    panel = Panel(state_dir, exit_id, public_ip, **providers)
    assets = Path(static_dir).resolve()
    tls = Tls(state_dir, public_ip, cert_dir)
    panel.fingerprint = tls.fingerprint

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, _format, *_args):  # noqa - BaseHTTPRequestHandler hook
            pass  # В журнале не бывает адресов, cookie, паролей и профилей.

        def send(self, code, body=b'', content_type='application/json; charset=utf-8', **headers):
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            headers = {'Content-Type': content_type, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store',
                       'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                       'Content-Security-Policy': CSP, **{k.replace('_', '-'): v for k, v in headers.items()}}
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def static(self, name):
            path = (assets / name).resolve()
            if not path.is_relative_to(assets) or not path.is_file():
                return self.send(404)
            self.send(200, path.read_bytes(), mimetypes.guess_type(path)[0] or 'application/octet-stream')

        def do_GET(self):  # noqa - BaseHTTPRequestHandler hook
            with panel.lock:
                path = urlsplit(self.path).path
                parts = path.split('/')
                if len(parts) == 3 and parts[1] == 'start' and panel.start:
                    code, text = START_PAGES.get(panel.start(parts[2]), START_PAGES['invalid'])
                    return self.send(code, text.encode(), 'text/plain; charset=utf-8')
                if panel.link_only:
                    return self.send(404)
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
                if not panel.session(self.headers.get('Cookie', '')):
                    return self.send(401)
                if path == '/api/state':
                    return self.send(200, panel.view())
                if len(parts) == 5 and parts[1:3] == ['api', 'profile'] and parts[3].isdigit():
                    self.profile(panel.profile(int(parts[3])), parts[3], parts[4])
                else:
                    self.send(404)

        def profile(self, profile, device, kind):
            if profile and kind == 'uri':
                return self.send(200, dict(uri=profile['uri']))
            if profile and kind == 'qr':
                return self.send(200, profile['qr_png'], 'image/png')
            file = Path(profile['file']).resolve() if profile and kind == 'file' else None
            if file and file.is_relative_to(panel.directory.resolve()) and file.is_file():
                return self.send(200, file.read_bytes(), 'application/octet-stream',
                                 Content_Disposition=f'attachment; filename="hysteria2-{device}{file.suffix}"')
            self.send(404)

        def do_POST(self):  # noqa - BaseHTTPRequestHandler hook
            with panel.lock:
                if panel.link_only:
                    return self.send(404)
                # Точный same-origin JSON - защита входа, выхода и рассылки от CSRF.
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
                    cookie = f'{COOKIE}={session}; Secure; HttpOnly; SameSite=Strict; Path=/'
                    return self.send(code, dict(ok=code == 200), **({'Set_Cookie': cookie} if session else {}))
                session = panel.session(self.headers.get('Cookie', ''))
                if not session:
                    return self.send(401)
                if self.path == '/api/logout':
                    panel.sessions.pop(session, None)
                    return self.send(200, dict(ok=True),
                                     Set_Cookie=f'{COOKIE}=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                if self.path == '/api/broadcast':
                    return self.broadcast(body.get('text'))
                self.send(404)

        def broadcast(self, message):
            if not isinstance(message, str) or not 0 < len(message.strip()) <= 4096:
                return self.send(400)
            if not panel.view()['bot_ready']:
                return self.send(503, dict(error='Рассылка недоступна'))
            try:
                panel.broadcast(message.strip(), panel.auth['generation'])
            except Exception:
                return self.send(503, dict(error='Не удалось поставить сообщение в очередь'))
            self.send(202, dict(message='Сообщение поставлено в очередь. Результат доставки обновляется автоматически.'))

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
            # Отвергнутый сертификат, сканер или обрыв - одна строка без трассы.
            if isinstance(failure, (ssl.SSLError, ConnectionError, TimeoutError)):
                LOG.info('emergency panel: connection from %s dropped: %s', client_address[0],
                         getattr(failure, 'reason', None) or type(failure).__name__)
                return
            super().handle_error(request, client_address)

    server = Server(address, Handler, bind_and_activate=False)
    server.socket = tls.context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    try:
        server.server_bind()
        server.server_activate()
    except BaseException:
        server.server_close()
        raise
    server.panel = panel

    def refresh_tls():
        changed = tls.reload()
        panel.fingerprint = tls.fingerprint
        if changed:
            LOG.info('Emergency panel TLS certificate renewed')
        return changed

    server.refresh_tls = refresh_tls
    return server
