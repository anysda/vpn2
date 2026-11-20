"""Панель только по HTTPS: без сертификата CA - на хранимой самоподписанной паре, CA встаёт перезагрузкой Caddy.

Настоящий Caddy гоняет Caddyfile стадии панели на её же файлах; заменены порты и выпуск на сайте
loopback (его сертификат делает openssl). `systemctl reload caddy` - настоящий `caddy reload`.
"""
import hashlib
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
from vpn2 import tls

LE = 'https://acme-v02.api.letsencrypt.org/directory'
ISSUER = 'acme-v02.api.letsencrypt.org-directory'
# Политика выпуска стадии, но каталог ACME недоступен: Caddy отказывает и ждёт, ответчик HTTP-01 есть.
ACME_DOWN, ACME_DOWN_ISSUER = 'http://127.0.0.1:1/directory', '127.0.0.1-1-directory'
IP = '192.0.2.1'
DOMAIN = 'vpn.example.test'
MARK = 'vpn2-panel-backend'
HOUR = 3600
PATHS = ('/', '/login', '/api/version', '/api/auth/session')
NOTICE = 'TLS: сертификат самоподписанный, отпечаток SHA-256 '


def stamp(seconds):
    return time.strftime('%y%m%d%H%M%SZ', time.gmtime(seconds))


def make_cert(directory, host, start, end):
    """Лист с SAN узла и заданным сроком (прошлые даты можно) - как выпущенный CA."""
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


def request(port, host, path, tls_on=False, sni=None):
    """(код, Location, тело); код None, когда соединение оборвано."""
    sock = socket.create_connection(('127.0.0.1', port), timeout=5)
    try:
        if tls_on:
            # Как `curl -k` и браузер после «принять риск»: самоподписанная пара годится.
            sock = ssl._create_unverified_context().wrap_socket(sock, server_hostname=sni)
        sock.sendall(f'GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
        response = http.client.HTTPResponse(sock)
        response.begin()
        return response.status, response.getheader('Location', ''), response.read().decode(errors='replace')
    except (ssl.SSLError, ConnectionError, http.client.HTTPException) as error:
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
        for name in ('docker', 'journalctl'):
            (stubs / name).write_text(f'#!/bin/sh\necho "{name} $*" >> {self.dir}/commands.log\n')
        # reload.sh кладёт запущенный Caddy: без него reload только записан.
        (stubs / 'systemctl').write_text(f'#!/bin/sh\necho "systemctl $*" >> {self.dir}/commands.log\n'
                                         f'[ "$1 $2" != "reload caddy" ] || [ ! -f {self.dir}/reload.sh ] '
                                         f'|| exec sh {self.dir}/reload.sh\n')
        # Всё, что попросили записать через `logger`, в журнал юнита vpn2-tls не попадает.
        (stubs / 'logger').write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {self.dir}/logger.log\n')
        (stubs / 'caddy').write_text(f'#!/bin/sh\nexec {caddy()} "$@"\n')
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{stubs}:{os.environ["PATH"]}', HOME=str(self.dir / 'home'),
                        PYTHONPATH=str(SRC), XDG_DATA_HOME=str(self.dir / 'validate-data'),
                        XDG_CONFIG_HOME=str(self.dir / 'validate-config'))
        self.journal = []
        self.deploy()

    def deploy(self):
        result = panel_tls(self.dir, self.domain, self.env, entry=IP)
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

    def state(self):
        return json.loads((self.dir / 'etc/vpn2/tls/state.json').read_text())

    def commands(self):
        log = self.dir / 'commands.log'
        return log.read_text().splitlines() if log.exists() else []

    def issue(self, start, end):
        """В хранилище Caddy эта пара лежит как выпущенный удостоверяющим центром сертификат узла."""
        crt, key = make_cert(self.dir, self.host, start, end)
        name = self.host.lower()
        target = self.storage / 'certificates' / ISSUER / name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy(crt, target / f'{name}.crt')
        shutil.copy(key, target / f'{name}.key')
        (target / f'{name}.json').write_text(json.dumps({'sans': [self.host], 'issuer_data': {'ca': LE}}))
        return crt

    def challenge(self, token, key_authorization):
        """Идущий вызов HTTP-01, как его хранит Caddy для распределённого решателя."""
        target = self.storage / 'acme' / ACME_DOWN_ISSUER / 'challenge_tokens'
        target.mkdir(parents=True, exist_ok=True)
        (target / f'{self.host}.json').write_text(json.dumps({
            'type': 'http-01', 'url': '', 'status': 'pending', 'token': token, 'keyAuthorization': key_authorization,
            'identifier': {'type': 'ip' if self.host == IP else 'dns', 'value': self.host}}))

    def serve(self):
        return Proxy(self)


class Proxy:
    """Настоящий Caddy на Caddyfile входа: пара сайта - живые файлы, reload - через admin API."""

    def __init__(self, entry):
        self.entry = entry

    def __enter__(self):
        self.backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        caddy_dir = self.entry.dir / 'etc/caddy'
        run = Path(tempfile.mkdtemp(dir=self.entry.dir))
        source = (caddy_dir / 'Caddyfile').read_text()
        assert source.startswith('{\n'), source[:200]
        (run / 'vpn2-tls.conf').write_text((caddy_dir / 'vpn2-tls.conf').read_text().replace(LE, ACME_DOWN))
        source = source.replace(f'import {caddy_dir}/vpn2-tls.conf', f'import {run}/vpn2-tls.conf')
        source = source.replace('127.0.0.1:51821', f'127.0.0.1:{self.backend.server_port}')
        self.log = run / 'caddy.log'
        env = dict(self.entry.env, XDG_DATA_HOME=str(self.entry.storage.parent), XDG_CONFIG_HOME=str(run))

        def start(http, https, loop, admin):
            site = re.sub(r':8443 \{', f':{loop} {{', source)
            site = site.replace('https://:443 {', f'https://:{https} {{')
            site = re.sub(r'^:80 \{', f':{http} {{', site, flags=re.M)
            (run / 'Caddyfile').write_text(f'{{\n    admin 127.0.0.1:{admin}\n    http_port {http}\n'
                                           f'    https_port {https}\n' + site[2:])
            (self.entry.dir / 'reload.sh').write_text(
                f'exec {caddy()} reload --config {run}/Caddyfile --address 127.0.0.1:{admin} --force\n')
            with self.log.open('w') as log:
                return subprocess.Popen([caddy(), 'run', '--config', str(run / 'Caddyfile'), '--adapter', 'caddyfile'],
                                        env=env, stdout=log, stderr=subprocess.STDOUT)

        self.process, (self.http, self.https, _loop, _admin) = start_on_free_ports(
            start, 4, socket.SOCK_STREAM, lambda: self.log.read_text()[-3000:])
        return self

    def __exit__(self, *_exc):
        (self.entry.dir / 'reload.sh').unlink(missing_ok=True)
        self.process.terminate()
        self.process.wait(timeout=20)
        self.backend.shutdown()
        self.backend.server_close()

    def get(self, host, path, tls_on=False, sni=None):
        return request(self.https if tls_on else self.http, host, path, tls_on, sni)

    def served(self):
        """Отпечаток пары, которую видит браузер на сайте панели (без SNI на IP, с SNI на домене)."""
        sock = socket.create_connection(('127.0.0.1', self.https), timeout=5)
        with ssl._create_unverified_context().wrap_socket(sock, server_hostname=self.entry.domain or None) as wrapped:
            der = wrapped.getpeercert(binary_form=True)
        return ':'.join(f'{byte:02X}' for byte in hashlib.sha256(der).digest())

    def assert_http_redirects(self, *hosts):
        """По HTTP только уход на https, ни страниц панели, ни данных API."""
        for host in hosts:
            for path in PATHS:
                status, location, body = self.get(host, path)
                label = f'http://{host}{path}: {status} {location} {body[:80]!r}'
                assert MARK not in body and status in (301, 308), label
                assert location == f'https://{self.entry.host}{path}', label

    def assert_https_open(self):
        for path in PATHS:
            status, _location, body = self.get(self.entry.host, path, tls_on=True, sni=self.entry.domain or None)
            assert (status, body) == (200, MARK), f'https://{self.entry.host}{path}: {status} {body[:200]}'


@pytest.fixture(params=['', DOMAIN], ids=['ip', 'domain'])
def entry(request, tmp_path):
    return Entry(tmp_path, request.param)


def test_without_ca_certificate_panel_is_served_on_the_stable_self_signed_pair(entry):
    output = entry.tick(initial=True)
    own = entry.state()['self_signed']
    assert NOTICE + own + '; ' in output
    with entry.serve() as proxy:
        assert proxy.served() == own, 'напечатанный отпечаток - тот, что видит браузер'
        proxy.assert_https_open()
        proxy.assert_http_redirects(entry.host, IP)
    # Повторный деплой и таймер пару не меняют: отпечаток, однажды показанный, остаётся верным.
    entry.deploy()
    entry.tick(initial=True)
    entry.tick()
    assert entry.state()['self_signed'] == own
    assert not [line for line in entry.commands() if line.startswith('docker')]


def test_ca_certificate_replaces_self_signed_by_reload_and_expiry_brings_it_back(entry):
    entry.tick(initial=True)
    own = entry.state()['self_signed']
    now = time.time()
    with entry.serve() as proxy:
        pid = proxy.process.pid
        ca = entry.issue(now - HOUR, now + 6 * 24 * HOUR)
        assert NOTICE not in entry.tick()
        assert proxy.served() == tls.fingerprint(ca) and 'self_signed' not in entry.state()
        proxy.assert_https_open()
        # Обновление не удалось, сертификат истёк: назад та же самоподписанная пара, не закрытая панель.
        entry.issue(now - 8 * 24 * HOUR, now - HOUR)
        assert NOTICE + own in entry.tick()
        assert proxy.served() == own
        proxy.assert_https_open()
        renewed = entry.issue(now - HOUR, now + 6 * 24 * HOUR)
        entry.tick()
        assert proxy.served() == tls.fingerprint(renewed)
        assert (proxy.process.poll(), proxy.process.pid) == (None, pid), 'Caddy перечитал пару, не перезапущен'
    assert entry.commands().count('systemctl reload caddy') == 3
    assert not [line for line in entry.commands() if line.startswith('docker')]


def test_upgrade_with_ca_certificate_serves_it_from_the_first_start(tmp_path):
    """Обновление с 1.2.0 с рабочим сертификатом: хранилище Caddy то же, самоподписанная пара не появляется."""
    entry = Entry(tmp_path, '')
    ca = entry.issue(time.time() - HOUR, time.time() + 6 * 24 * HOUR)
    entry.deploy()
    with entry.serve() as proxy:
        assert proxy.served() == tls.fingerprint(ca)
    assert NOTICE not in entry.tick(initial=True)
    assert 'self_signed' not in entry.state()


def test_foreign_host_on_the_entry_ip_gets_no_panel(tmp_path):
    entry = Entry(tmp_path, DOMAIN)
    entry.tick(initial=True)
    with entry.serve() as proxy:
        for sni in (None, IP):
            for path in PATHS:
                status, _location, body = proxy.get(IP, path, tls_on=True, sni=sni)
                assert MARK not in body and status is None, f'https://{IP}{path} sni={sni}: {status}'
        proxy.assert_https_open()


def test_http01_challenge_is_answered_on_self_signed_and_ca_pair(entry):
    entry.challenge('vpn2-token', 'vpn2-token.key-authorization')
    answer = (200, 'vpn2-token.key-authorization')
    entry.tick(initial=True)
    with entry.serve() as proxy:
        assert proxy.get(entry.host, '/.well-known/acme-challenge/vpn2-token')[::2] == answer, 'self-signed'
        entry.issue(time.time() - HOUR, time.time() + 6 * 24 * HOUR)
        entry.tick()
        assert proxy.get(entry.host, '/.well-known/acme-challenge/vpn2-token')[::2] == answer, 'ca'


@pytest.mark.parametrize('installed', [False, True], ids=['timer', 'installation'])
def test_fallback_is_in_the_unit_journal_once(installed, tmp_path):
    entry = Entry(tmp_path, '')
    if installed:
        assert NOTICE in entry.tick(initial=True)
    for _ in range(3):
        entry.tick()
    lines = [line for line in entry.journal if NOTICE in line]
    assert len(lines) == 1, entry.journal
    assert not (entry.dir / 'logger.log').exists()


@pytest.mark.parametrize('domain', ['', DOMAIN], ids=['ip', 'domain'])
def test_own_site_on_the_ip_survives_the_stage_with_its_pair(domain, tmp_path):
    """Мастер установки: свой сайт на IP entry со своей парой до стадии 30 и после неё, в том числе когда стадия
    перевела панель на домен (браузер по IP приходит без SNI); неверный откатывается; /api/version - панели."""
    entry = Entry(tmp_path, '')
    entry.tick(initial=True)
    caddy_dir = entry.dir / 'etc/caddy'
    for suffix in ('crt', 'key'):
        shutil.copy(caddy_dir / f'vpn2-site.{suffix}', caddy_dir / f'wizard.{suffix}')

    def serve(routes, site=''):
        code = (f'from vpn2 import tls; tls.serve_routes("wizard", {routes!r}, {str(caddy_dir)!r}, {site!r})')
        return subprocess.run([sys.executable, '-c', code], env=entry.env, capture_output=True, text=True, timeout=60)

    routes = '@wizard not path /api/version\nroute @wizard {\n    respond "wizard" 200\n}\n'
    site = (f'https://{IP} {{\n    tls {caddy_dir}/wizard.crt {caddy_dir}/wizard.key\n'
            '    handle /api/version {\n        import to_panel\n    }\n    respond "wizard" 200\n}\n')

    def served(proxy):
        with socket.create_connection(('127.0.0.1', proxy.https), timeout=5) as sock, \
                ssl._create_unverified_context().wrap_socket(sock) as wrapped:
            return wrapped.getpeercert(True)

    with entry.serve() as proxy:
        assert serve(routes, site).returncode == 0
        assert proxy.get(IP, '/', tls_on=True)[::2] == (200, 'wizard')
        assert serve(routes[:-2], site).returncode != 0, 'неверный блок принят'
        assert proxy.get(IP, '/', tls_on=True)[::2] == (200, 'wizard')
    entry.domain, entry.host = domain, domain or IP
    entry.deploy()
    with entry.serve() as proxy:
        assert proxy.get(IP, '/', tls_on=True)[::2] == (200, 'wizard'), 'стадия 30 сняла сайт мастера'
        assert proxy.get(IP, '/api/version', tls_on=True)[::2] == (200, MARK)
        assert served(proxy) == ssl.PEM_cert_to_DER_cert((caddy_dir / 'wizard.crt').read_text()), 'не пара IP'
        if domain:
            assert proxy.get(DOMAIN, '/api/version', tls_on=True, sni=DOMAIN)[::2] == (200, MARK)
        assert serve('').returncode == 0
        proxy.assert_https_open()


def test_pair_swapped_by_the_timer_restarts_nothing_on_the_next_stage():
    """Отпечаток перезапуска Caddy - только Caddyfile: окно и пару меняет таймер, повторный деплой не видит правки."""
    from unittest.mock import patch
    from vpn2 import firewall, svc
    from vpn2.containers import panel
    calls = []
    with patch.object(firewall, 'ufw_reconcile'), patch.object(svc, 'restart_if_changed', return_value=False) as restart, \
            patch.object(panel.subprocess, 'run', side_effect=lambda argv, **_: calls.append(argv)
                         or subprocess.CompletedProcess(argv, 0)):
        assert panel.caddy() is False
        assert ['systemctl', 'reload', 'caddy'] not in calls
        assert panel.caddy(reload=True) is True
    assert restart.call_args[0][1][2:] == [panel.CADDY[0]]
    assert calls.count(['systemctl', 'reload', 'caddy']) == 1
