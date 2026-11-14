"""Панель открывается только по HTTPS с годным сертификатом, причина закрытия - в журнале vpn2-tls.

Настоящий Caddy гоняет Caddyfile стадии панели и вставки vpn2 tls. Заменена только политика
выпуска (vpn2-tls.conf): её сертификат делает openssl, и самоподписанный или истёкший
сертификат, который Caddy всё ещё отдаёт, не должен открыть панель.
"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from panel_support import SRC, caddy, panel_tls
from ports_support import start_on_free_ports

LE = 'https://acme-v02.api.letsencrypt.org/directory'
ISSUER = 'acme-v02.api.letsencrypt.org-directory'
IP = '192.0.2.1'
DOMAIN = 'vpn.example.test'
MARK = 'vpn2-panel-backend'
HOUR = 3600
PATHS = ('/', '/login', '/api/version', '/api/auth/session')
CLOSED = 'TLS: панель закрыта: '
OPEN = 'import to_panel\n'


def stamp(seconds):
    return time.strftime('%y%m%d%H%M%SZ', time.gmtime(seconds))


def make_cert(directory, host, start, end):
    """Самоподписанный лист с SAN узла и заданным сроком (прошлые даты можно)."""
    work = Path(tempfile.mkdtemp(dir=directory))
    san = f'IP:{host}' if re.fullmatch(r'[0-9.]+', host) else f'DNS:{host}'
    (work / 'index.txt').write_text('')
    (work / 'serial').write_text('01\n')
    (work / 'ca.cnf').write_text(
        f'[ca]\ndefault_ca = local\n[local]\ndatabase = {work}/index.txt\nnew_certs_dir = {work}\n'
        f'serial = {work}/serial\ndefault_md = sha256\npolicy = any\nunique_subject = no\n'
        f'x509_extensions = leaf\n[any]\ncommonName = supplied\n[leaf]\nsubjectAltName = {san}\nbasicConstraints = CA:false\n')
    key, csr, crt = work / 'key.pem', work / 'req.csr', work / 'cert.pem'
    subprocess.run(['openssl', 'req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
                    '-keyout', key, '-subj', f'/CN={host}', '-out', csr], check=True, capture_output=True)
    subprocess.run(['openssl', 'ca', '-batch', '-notext', '-config', work / 'ca.cnf', '-selfsign', '-keyfile', key,
                    '-in', csr, '-out', crt, '-startdate', stamp(start), '-enddate', stamp(end)],
                   check=True, capture_output=True)
    return crt, key


class Backend(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - имя задаёт BaseHTTPRequestHandler
        self.send_response(200)
        self.send_header('Content-Length', str(len(MARK)))
        self.end_headers()
        self.wfile.write(MARK.encode())

    def log_message(self, *_args):
        pass


def request(port, host, path, tls=False, sni=None):
    """(код, Location, тело); код None, когда TLS-рукопожатие отвергнуто."""
    sock = socket.create_connection(('127.0.0.1', port), timeout=5)
    try:
        if tls:
            # Как `curl -k`: клиент принимает истёкший и самоподписанный сертификат.
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
    """TLS-часть стадии панели во временном корне; vpn2 tls гоняется по ней, журналы собираются."""

    def __init__(self, directory, domain):
        self.dir, self.domain, self.host = Path(directory), domain, domain or IP
        self.storage = self.dir / 'var/lib/caddy/.local/share/caddy'
        stubs = self.dir / 'bin'
        stubs.mkdir(parents=True)
        for name in ('docker', 'systemctl', 'journalctl'):
            (stubs / name).write_text(f'#!/bin/sh\necho "{name} $*" >> {self.dir}/commands.log\n')
        # Всё, что попросили записать через `logger`, в журнал юнита vpn2-tls не попадает.
        (stubs / 'logger').write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {self.dir}/logger.log\n')
        (stubs / 'caddy').write_text(f'#!/bin/sh\nexec {caddy()} "$@"\n')
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{stubs}:{os.environ["PATH"]}', HOME=str(self.dir / 'home'),
                        PYTHONPATH=str(SRC), XDG_DATA_HOME=str(self.dir / 'validate-data'),
                        XDG_CONFIG_HOME=str(self.dir / 'validate-config'))
        self.journal = []
        result = panel_tls(self.dir, domain, self.env, entry=IP)
        assert result.returncode == 0, result.stdout + result.stderr

    def tick(self, initial=False):
        """Запуск vpn2-tls.service (его вывод - журнал юнита) или --initial установки (вывод в терминал)."""
        command = [sys.executable, '-m', 'vpn2.cli', 'tls', '--config', str(self.dir / 'etc/vpn2/tls/config.json')]
        result = subprocess.run(command + (['--initial', '--wait', '0'] if initial else []),
                                capture_output=True, text=True, timeout=120, env=self.env)
        assert result.returncode == 0, result.stdout + result.stderr
        if not initial:
            self.journal += result.stdout.splitlines()
        return result.stdout

    def closed(self):
        return [line for line in self.journal if CLOSED in line]

    def logged_outside(self):
        log = self.dir / 'logger.log'
        return log.read_text().splitlines() if log.exists() else []

    def opened(self):
        return (self.dir / 'etc/caddy/vpn2-panel.conf').read_text() == OPEN

    def issue(self, start, end):
        """В хранилище Caddy эта пара лежит как выпущенный удостоверяющим центром сертификат узла."""
        crt, key = make_cert(self.dir, self.host, start, end)
        name = self.host.lower()
        target = self.storage / 'certificates' / ISSUER / name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy(crt, target / f'{name}.crt')
        shutil.copy(key, target / f'{name}.key')
        (target / f'{name}.json').write_text(json.dumps({'sans': [self.host], 'issuer_data': {'ca': LE}}))
        return crt, key

    def challenge(self, token, key_authorization):
        """Идущий вызов HTTP-01, как его хранит Caddy для распределённого решателя."""
        target = self.storage / 'acme' / ISSUER / 'challenge_tokens'
        target.mkdir(parents=True, exist_ok=True)
        (target / f'{self.host}.json').write_text(json.dumps({
            'type': 'http-01', 'url': '', 'status': 'pending', 'token': token, 'keyAuthorization': key_authorization,
            'identifier': {'type': 'ip' if self.host == IP else 'dns', 'value': self.host}}))

    def serve(self, cert):
        return Proxy(self, cert)


class Proxy:
    """Настоящий Caddy на файлах входа; vpn2-tls.conf отдаёт заданную пару."""

    def __init__(self, entry, cert):
        self.entry, self.cert = entry, cert

    def __enter__(self):
        self.backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        run, source = Path(tempfile.mkdtemp(dir=self.entry.dir)), self.entry.dir / 'etc/caddy'
        for path in source.iterdir():
            if path.is_file():
                (run / path.name).write_text(path.read_text().replace(str(source), str(run)))
        (run / 'vpn2-tls.conf').write_text(f'tls {self.cert[0]} {self.cert[1]}\n')
        caddyfile = (run / 'Caddyfile').read_text().replace('127.0.0.1:51821', f'127.0.0.1:{self.backend.server_port}')
        assert caddyfile.startswith('{\n'), caddyfile[:200]
        self.log = run / 'caddy.log'
        # Хранилище входа: из него Caddy читает токены HTTP-01.
        env = dict(self.entry.env, XDG_DATA_HOME=str(self.entry.storage.parent), XDG_CONFIG_HOME=str(run))

        def start(http, https):
            site = re.sub(r'^:80 \{', f':{http} {{', caddyfile, flags=re.M)
            (run / 'Caddyfile').write_text(f'{{\n    admin off\n    http_port {http}\n    https_port {https}\n' + site[2:])
            with self.log.open('w') as log:
                return subprocess.Popen([caddy(), 'run', '--config', str(run / 'Caddyfile'), '--adapter', 'caddyfile'],
                                        env=env, stdout=log, stderr=subprocess.STDOUT)

        self.process, (self.http, self.https) = start_on_free_ports(start, 2, socket.SOCK_STREAM,
                                                                    lambda: self.log.read_text()[-3000:])
        return self

    def __exit__(self, *_exc):
        self.process.terminate()
        self.process.wait(timeout=20)
        self.backend.shutdown()
        self.backend.server_close()

    def get(self, host, path, tls=False, sni=None):
        return request(self.https if tls else self.http, host, path, tls, sni)

    def assert_http_closed(self, *hosts):
        """По HTTP только уход на https или отказ, ни страниц панели, ни данных API."""
        for host in hosts:
            for path in PATHS:
                status, location, body = self.get(host, path)
                label = f'http://{host}{path}: {status} {location} {body[:80]!r}'
                assert MARK not in body and status is not None and status >= 300, label
                assert status >= 400 or location.startswith('https://'), label

    def assert_https_closed(self):
        for path in PATHS:
            status, _location, body = self.get(self.entry.host, path, tls=True, sni=self.entry.domain or None)
            assert MARK not in body and (status is None or status >= 400), f'https://{self.entry.host}{path}: {status}'

    def assert_https_open(self):
        for path in PATHS:
            status, _location, body = self.get(self.entry.host, path, tls=True, sni=self.entry.domain or None)
            assert (status, body) == (200, MARK), f'https://{self.entry.host}{path}'

    def assert_ip_bypass_closed(self):
        for path in PATHS:
            for sni in (None, IP):
                status, _location, body = self.get(IP, path, tls=True, sni=sni)
                assert MARK not in body, f'https://{IP}{path} sni={sni}: {status}'


@pytest.fixture(params=['', DOMAIN], ids=['ip', 'domain'])
def entry(request, tmp_path):
    return Entry(tmp_path, request.param)


def test_http_never_serves_panel_before_or_after_certificate(entry):
    now = time.time()
    with entry.serve(make_cert(entry.dir, entry.host, now - HOUR, now + 6 * 24 * HOUR)) as proxy:
        proxy.assert_http_closed(entry.host, IP)
    cert = entry.issue(now - HOUR, now + 6 * 24 * HOUR)
    entry.tick()
    with entry.serve(cert) as proxy:
        proxy.assert_http_closed(entry.host, IP)
        # Положительный контроль: тот же Caddy открывает панель по HTTPS.
        proxy.assert_https_open()


def test_not_issued_certificate_keeps_panel_closed_and_says_why(entry):
    now = time.time()
    output = entry.tick(initial=True)
    assert CLOSED + 'сертификат не выпущен' in output
    # Самоподписанная замена, которую Caddy может отдать, панель не открывает - ни после установки, ни по таймеру.
    self_signed = make_cert(entry.dir, entry.host, now - HOUR, now + 30 * 24 * HOUR)
    with entry.serve(self_signed) as proxy:
        proxy.assert_http_closed(entry.host, IP)
        proxy.assert_https_closed()
    entry.tick()
    with entry.serve(self_signed) as proxy:
        proxy.assert_https_closed()


def test_expired_certificate_on_running_entry_closes_panel_and_renewal_reopens_it(entry):
    now = time.time()
    cert = entry.issue(now - 5 * 24 * HOUR, now + 24 * HOUR)
    entry.tick()
    with entry.serve(cert) as proxy:
        proxy.assert_https_open()
    # Обновление не удалось, сертификат истёк, Caddy всё ещё его отдаёт.
    expired = entry.issue(now - 8 * 24 * HOUR, now - HOUR)
    output = entry.tick()
    with entry.serve(expired) as proxy:
        proxy.assert_https_closed()
        proxy.assert_http_closed(entry.host, IP)
    assert CLOSED in output
    # Позже обновление прошло: таймер открывает панель без переустановки.
    renewed = entry.issue(now - HOUR, now + 6 * 24 * HOUR)
    entry.tick()
    with entry.serve(renewed) as proxy:
        proxy.assert_https_open()


def test_entry_ip_instead_of_domain_does_not_open_panel(tmp_path):
    now = time.time()
    entry = Entry(tmp_path, DOMAIN)
    with entry.serve(make_cert(entry.dir, DOMAIN, now - HOUR, now + 6 * 24 * HOUR)) as proxy:
        proxy.assert_http_closed(IP)
        proxy.assert_ip_bypass_closed()
    cert = entry.issue(now - HOUR, now + 6 * 24 * HOUR)
    entry.tick()
    with entry.serve(cert) as proxy:
        proxy.assert_https_open()
        proxy.assert_http_closed(IP)
        proxy.assert_ip_bypass_closed()


def test_http01_challenge_is_answered_while_panel_is_closed_and_open(entry):
    now = time.time()
    entry.challenge('vpn2-token', 'vpn2-token.key-authorization')
    answer = (200, 'vpn2-token.key-authorization')
    with entry.serve(make_cert(entry.dir, entry.host, now - HOUR, now + 6 * 24 * HOUR)) as proxy:
        assert proxy.get(entry.host, '/.well-known/acme-challenge/vpn2-token')[::2] == answer, 'closed'
    cert = entry.issue(now - HOUR, now + 6 * 24 * HOUR)
    entry.tick()
    with entry.serve(cert) as proxy:
        assert proxy.get(entry.host, '/.well-known/acme-challenge/vpn2-token')[::2] == answer, 'open'
        proxy.assert_https_open()


@pytest.mark.parametrize('installed', [False, True], ids=['timer', 'installation'])
def test_closed_reason_is_in_the_unit_journal_once(entry, installed):
    if installed:
        assert CLOSED + 'сертификат не выпущен' in entry.tick(initial=True)
    entry.tick()
    assert entry.closed() == [CLOSED + 'сертификат не выпущен; откроется сама после выпуска сертификата'], entry.journal
    # Перезапуски и запуски таймера без смены состояния молчат; `logger` из установки в юнит не попадает.
    entry.tick()
    entry.tick()
    assert len(entry.closed()) == 1, entry.journal
    assert entry.logged_outside() == []


def test_each_state_change_logs_once_and_stale_reason_is_not_written(tmp_path):
    # Первый вход уже записал причину; на втором сертификат выпущен между установкой и первым запуском службы.
    running, installed = Entry(tmp_path / 'running', ''), Entry(tmp_path / 'installed', '')
    running.tick()
    assert CLOSED + 'сертификат не выпущен' in installed.tick(initial=True)
    # Скоро истекающий сертификат открывает панель. Нагруженная машина может пропустить запуск таймера.
    end = time.time() + 40
    for entry in (running, installed):
        entry.issue(time.time() - HOUR, end)
        for _ in range(5):
            entry.tick()
            if entry.opened() or time.time() > end - 5:
                break
        assert entry.opened(), 'certificate did not open the panel in time'
    assert [len(running.closed()), len(installed.closed())] == [1, 0], (running.journal, installed.journal)
    time.sleep(max(0, end + 1 - time.time()))
    for entry in (running, installed):
        entry.tick()
        entry.tick()
        assert entry.closed()[-1].startswith(CLOSED + 'сертификат истёк'), entry.journal
        assert entry.logged_outside() == []
    assert [len(running.closed()), len(installed.closed())] == [2, 1]
