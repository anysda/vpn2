#!/usr/bin/env python3
"""Live probe: real Caddy + Pebble (ACME test CA) + the bot outbox.

Entry without a domain: first issuance failure -> HTTP stays and the bot
queue gets an alert; IP certificate -> HTTPS with a Secure cookie; forced
renewal window -> new certificate; ACME blocked -> alert with expiry;
unblocked -> "updated". Exit role: same code, certificate exported for
sing-box while TCP 443 stays free. No public CA, no Telegram.

CADDY_BIN=<caddy 2.11.7> DOCKER='sudo -n docker' <venv with telegram/requirements.txt>/bin/python \
    infra/tests/probe_tls_live.py
"""

import asyncio
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shlex
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'telegram/tests'), str(ROOT / 'telegram')]
from support import load_bot  # noqa: E402
from outbox import Outbox  # noqa: E402

spec = importlib.util.spec_from_file_location('vpn2_tls', ROOT / 'infra/lib/vpn2_tls.py')
tls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tls)
DOCKER = shlex.split(os.environ.get('DOCKER', 'docker'))
CADDY = os.environ.get('CADDY_BIN', 'caddy')
PEBBLE = os.environ.get('PEBBLE_IMAGE', 'ghcr.io/letsencrypt/pebble:latest')
PANEL = os.environ.get('TLS_PANEL_IMAGE', 'ghcr.io/anysda/vpn2/panel:dev')
HOST = '127.0.0.1'


def docker(*args):
    return subprocess.run([*DOCKER, *args], check=True, capture_output=True, text=True, timeout=120).stdout


def free_port():
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def wait_for(check, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(.5)
    raise AssertionError('condition did not become ready within timeout')


def get(url, context=None):
    with urllib.request.urlopen(url, context=context, timeout=5) as response:
        return response.status


class Caddy:
    """Host Caddy process; its stderr is the journal vpn2_tls.py reads."""

    def __init__(self, root, caddyfile):
        self.root, self.caddyfile, self.process, self.seen = root, caddyfile, None, 0
        self.log = root / 'caddy.log'

    def start(self):
        env = dict(os.environ, XDG_DATA_HOME=str(self.root / 'data'), XDG_CONFIG_HOME=str(self.root / 'config'))
        with self.log.open('a') as log:
            self.process = subprocess.Popen([CADDY, 'run', '--config', str(self.caddyfile)],
                                            stdout=log, stderr=log, env=env)

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=30)
            self.process = None

    def records(self, _state=None):
        lines = self.log.read_text().splitlines()
        parsed = []
        for line in lines[self.seen:]:
            try:
                parsed.append(json.loads(line))
            except ValueError:
                pass
        self.seen = len(lines)
        return parsed

    def services(self, original):
        """systemctl/docker calls of vpn2_tls.py mapped onto this probe."""
        def run(args, **kwargs):
            if args[0] == 'caddy':
                args = [CADDY, *args[1:]]
            elif args[:2] == ['systemctl', 'reload']:
                args = [CADDY, 'reload', '--config', str(self.caddyfile)]
            elif args[0] == 'docker':
                args = [*DOCKER, *args[1:]]
            return original(args, **kwargs)
        return run


def take(queue):
    """Next bot message, as the delivery loop would send it to Telegram."""
    item = queue.claim()
    queue.remove(item['id'])
    return json.loads(item['payload'])


def tick(config, state_path, caddy, initial=False):
    with patch.object(tls, 'journal', caddy.records), \
            patch.object(tls.subprocess, 'run', caddy.services(subprocess.run)):
        return tls.tick(config, state_path, initial)


def force_renewal(config, caddy):
    """Move the window to "renew now" and the CA's ARI time with it (ARI
    takes precedence over the local ratio), then restart to reload storage."""
    path = Path(config['caddy_dir']) / 'vpn2-tls.conf'
    text = path.read_text()
    current = text.split('renewal_window_ratio ')[1].split()[0]
    tls.atomic(path, text.replace(f'renewal_window_ratio {current}', 'renewal_window_ratio 0.999990000'), 0o644)
    item = tls.current_certificate(config, time.time())
    metadata = Path(item['cert']).with_suffix('.json')
    data = json.loads(metadata.read_text())
    ari = data['issuer_data'].get('renewal_info')
    if ari:
        past = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 60))
        ari['suggestedWindow'] = {'start': past, 'end': past}
        ari['_selectedTime'] = past
        tls.atomic(metadata, json.dumps(data))
    caddy.stop()
    caddy.start()


def newer(config, previous):
    item = tls.current_certificate(config, time.time())
    return item if item and item['fingerprint'] != previous['fingerprint'] else None


def stage_block(root):
    """TLS part of stage 30 with system paths moved into the probe directory."""
    stage = (ROOT / 'infra/scripts/30-frontend.sh').read_text()
    start = stage.index('if [[ -n "${PANEL_DOMAIN:-}" ]]; then', stage.index('TLS_LIB='))
    block = stage[start:stage.index('\nufw_sync_rules', start)]
    for path, moved in (('/usr/local/lib/vpn2/vpn2_tls.py', ROOT / 'infra/lib/vpn2_tls.py'),
                        ('/etc/vpn2/tls/config.json', root / 'config.json'),
                        ('/etc/vpn2/tls/panel/secure', root / 'panel/secure'),
                        ('/var/lib/caddy/.local/share/caddy', root / 'data/caddy'),
                        ('/etc/caddy', root)):
        block = block.replace(path, str(moved))
    # Rollback of the snippets is covered by test_tls; here only the rendered result matters.
    return 'panel_guard_files() { :; }\n' + block


def panel_scenario(root, ports, queue, bot_url, ca):
    http_port, https_port, admin_port, panel_port = ports
    public = f'https://{HOST}:{https_port}'
    subprocess.run(['bash', '-eu', '-c', stage_block(root)], check=True, capture_output=True,
                   env=dict(os.environ, PANEL_DOMAIN='', ENTRY_HOST=HOST, HOST_TAG='ru',
                            TLS_ACME_URL=ca['url'], TLS_ACME_ROOT=str(ca['root'])))
    config = json.loads((root / 'config.json').read_text())
    # Test-only: bot endpoint and secret of the in-process bot, test ports.
    config.update(bot_url=bot_url, bot_secret=str(root / 'bot-secret'), panel_container=ca['panel'],
                  output_dir=str(root / 'ip'))
    tls.atomic(root / 'bot-secret', ca['secret'])
    caddyfile = (root / 'Caddyfile').read_text()
    assert f'https://{HOST} {{' in caddyfile and 'profile shortlived' in (root / 'vpn2-tls.conf').read_text()
    caddyfile = caddyfile.replace('auto_https disable_redirects', (
        f'auto_https disable_redirects\n    admin {HOST}:{admin_port}\n    renew_interval 1s\n'
        f'    http_port {http_port}\n    https_port {https_port}'))
    caddyfile = caddyfile.replace(':80 {', f':{http_port} {{').replace(
        f'https://{HOST} {{', f'{public} {{').replace('127.0.0.1:51821', f'127.0.0.1:{panel_port}')
    tls.atomic(root / 'Caddyfile', caddyfile, 0o644)
    docker('run', '-d', '--rm', '--name', ca['panel'], '--network', 'host',
           '-v', f'{root}/panel:/run/vpn2-tls:ro', '-e', f'PORT={panel_port}', '-e', 'HOST=127.0.0.1',
           '-e', 'NUXT_SESSION_PASSWORD=test-session-password-at-least-thirty-two-characters',
           '-e', 'NUXT_ADMIN_USER=admin', '-e', 'NUXT_ADMIN_PASSWORD=test-password-only',
           '-e', 'NUXT_DATABASE_URL=file:/tmp/test.sqlite', PANEL, 'sh', '-ec',
           'export NUXT_SESSION_COOKIE_SECURE=$(cat /run/vpn2-tls/secure); '
           'node scripts/migrate.mjs; exec node .output/server/index.mjs')
    caddy = Caddy(root, root / 'Caddyfile')
    state_path = root / 'state.json'
    try:
        caddy.start()
        wait_for(lambda: get(f'http://{HOST}:{http_port}/api/version'))
        wait_for(lambda: 'could not get certificate' in caddy.log.read_text())
        assert not tick(config, state_path, caddy, initial=True)
        assert get(f'http://{HOST}:{http_port}/api/version') == 200
        assert (root / 'panel/secure').read_text() == 'false\n'
        event = take(queue)
        assert 'ошибка выпуска' in event['text'] and 'сертификата нет' in event['text'], event
        print('1. first issuance, ACME down: HTTP 200 kept, Secure=false; outbox:', event['text'].splitlines()[0],
              '|', event['text'].splitlines()[-1], flush=True)

        docker('start', ca['name'])
        wait_for(lambda: get(ca['url'], ca['context']))
        first = wait_for(lambda: tls.current_certificate(config, time.time()))
        assert tick(config, state_path, caddy)
        context = ssl.create_default_context(cafile=str(ca['issued']()))
        wait_for(lambda: get(public + '/api/version', context))
        with socket.create_connection((HOST, http_port)) as raw:
            raw.sendall(b'GET /x HTTP/1.1\r\nHost: ' + HOST.encode() + b'\r\nConnection: close\r\n\r\n')
            redirect = raw.recv(4096).decode().splitlines()
        assert ' 301 ' in redirect[0], redirect[0]
        login = urllib.request.Request(public + '/api/auth/sign-in/username', headers={
            'Content-Type': 'application/json', 'Origin': public},
            data=json.dumps({'username': 'admin', 'password': 'test-password-only'}).encode())
        with urllib.request.urlopen(login, context=context) as response:
            cookie = ';'.join(response.headers.get_all('Set-Cookie', []))
        assert 'Secure' in cookie
        recovered = take(queue)
        assert 'обновлено' in recovered['text'], recovered
        lifetime = (first['end'] - first['start']) / 3600
        window = (root / 'vpn2-tls.conf').read_text().split('renewal_window_ratio ')[1].split()[0]
        print(f'2. IP certificate: HTTPS 200 verified, SAN IP:{HOST}, lifetime {lifetime:.0f}h, window ratio {window}, '
              f'HTTP -> {redirect[0].split(" ", 1)[1]}, Set-Cookie Secure, export {(root / "ip/fullchain.pem").exists()}; '
              f'outbox: {recovered["text"].splitlines()[0]}',
              flush=True)

        force_renewal(config, caddy)
        second = wait_for(lambda: newer(config, first))
        assert tick(config, state_path, caddy)
        assert (root / 'ip/fullchain.pem').read_text() == Path(second['cert']).read_text()
        print('3. renewal window forced: certificate replaced, exported pair switched, HTTPS 200:',
              get(public + '/api/version', context), flush=True)

        docker('pause', ca['name'])
        caddy.records()
        force_renewal(config, caddy)
        wait_for(lambda: tick(config, state_path, caddy) and tls.read_json(state_path, {}).get('failed'), 150)
        assert get(public + '/api/version', context) == 200
        event = take(queue)
        assert 'ошибка выпуска' in event['text'] and 'UTC' in event['text'], event
        print('4. ACME blocked on renewal: current HTTPS 200; outbox:', event['text'].splitlines()[-1], flush=True)

        docker('unpause', ca['name'])
        force_renewal(config, caddy)
        wait_for(lambda: newer(config, second), 150)
        tick(config, state_path, caddy)
        assert not tls.read_json(state_path, {})['failed']
        event = take(queue)
        assert 'обновлено' in event['text'], event
        with closing(queue.connect()) as db:
            left = db.execute('SELECT count(*) FROM events').fetchone()[0]
        assert left == 0, 'one alert per failure and one "updated" per recovery'
        print('5. ACME unblocked: new certificate; outbox:', event['text'].splitlines()[0], flush=True)
    finally:
        caddy.stop()


def exit_scenario(root, ports, ca):
    http_port, local_port, admin_port = ports
    subprocess.run(['python3', str(ROOT / 'infra/lib/vpn2_tls.py'), '--prepare', '--role', 'exit',
                    '--config', f'{root}/config.json', '--caddy-dir', str(root), '--host', HOST,
                    '--storage', f'{root}/data/caddy', '--output-dir', f'{root}/ip',
                    '--acme-url', ca['url'], '--acme-root', str(ca['root']),
                    '--local-port', str(local_port), '--cert-group', 'root'], check=True)
    config = json.loads((root / 'config.json').read_text())
    caddyfile = (root / 'Caddyfile').read_text().replace('auto_https disable_redirects', (
        f'auto_https disable_redirects\n    admin {HOST}:{admin_port}\n    http_port {http_port}'))
    tls.atomic(root / 'Caddyfile', caddyfile.replace(':80 {', f':{http_port} {{'), 0o644)
    caddy = Caddy(root, root / 'Caddyfile')
    try:
        caddy.start()
        cert = wait_for(lambda: tls.current_certificate(config, time.time()))
        assert tick(config, root / 'state.json', caddy, initial=True)
        pair = tls.certificate(root / 'ip/fullchain.pem', root / 'ip/privkey.pem', HOST, time.time())
        assert pair and pair['fingerprint'] == cert['fingerprint']
        with socket.socket() as probe:
            refused = probe.connect_ex(('127.0.0.2', local_port))
        assert refused, 'exit HTTPS must listen on loopback only'
        print(f'6. exit role: certificate exported to ip/fullchain.pem + ip/privkey.pem (SAN IP:{HOST}, '
              f'{(cert["end"] - cert["start"]) / 3600:.0f}h), Caddy HTTPS only on 127.0.0.1:{local_port}, '
              f'TCP 443 untouched', flush=True)
    finally:
        caddy.stop()


def main(root):
    bot = load_bot()
    queue = Outbox(str(root / 'outbox.sqlite'))

    class BotHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa - called by BaseHTTPRequestHandler
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            request = SimpleNamespace(headers=self.headers, app={bot.OUTBOX_KEY: queue},
                                      json=AsyncMock(return_value=data))
            response = asyncio.run(bot.handle_event(request))
            self.send_response(response.status)
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, *_args):  # noqa - called by BaseHTTPRequestHandler
            pass

    server = ThreadingHTTPServer((HOST, 0), BotHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    acme_port, management_port, http_port, https_port = (free_port() for _ in range(4))
    tag = f'vpn2-tls-{os.getpid()}'
    pebble = {'pebble': {'listenAddress': f'0.0.0.0:{acme_port}',
                         'managementListenAddress': f'0.0.0.0:{management_port}',
                         'certificate': '/test/certs/localhost/cert.pem',
                         'privateKey': '/test/certs/localhost/key.pem',
                         'httpPort': http_port, 'tlsPort': https_port,
                         'profiles': {'default': {'description': 'domain', 'validityPeriod': 90 * 86400},
                                      'shortlived': {'description': 'IP', 'validityPeriod': 160 * 3600}}}}
    tls.atomic(root / 'pebble.json', json.dumps(pebble), 0o644)

    def issued():
        with urllib.request.urlopen(f'https://localhost:{management_port}/roots/0', context=ca['context']) as answer:
            tls.atomic(root / 'issued-ca.pem', answer.read().decode(), 0o644)
        return root / 'issued-ca.pem'

    ca = {'name': f'{tag}-acme', 'panel': f'{tag}-panel', 'url': f'https://localhost:{acme_port}/dir',
          'root': root / 'pebble-ca.pem', 'secret': bot.SECRET, 'issued': issued}
    try:
        docker('create', '--name', ca['name'], '--network', 'host', '-e', 'PEBBLE_VA_NOSLEEP=1',
               '-e', 'PEBBLE_WFE_NONCEREJECT=0', '-v', f'{root}/pebble.json:/config.json:ro',
               PEBBLE, '-config', '/config.json')
        docker('cp', f'{ca["name"]}:/test/certs/pebble.minica.pem', str(ca['root']))
        ca['context'] = ssl.create_default_context(cafile=str(ca['root']))
        (root / 'entry').mkdir()
        panel_scenario(root / 'entry', (http_port, https_port, free_port(), free_port()), queue,
                       f'http://{HOST}:{server.server_port}/event', ca)
        (root / 'exit').mkdir()
        exit_scenario(root / 'exit', (http_port, free_port(), free_port()), ca)
    finally:
        for name in (ca['name'], ca['panel']):
            subprocess.run([*DOCKER, 'rm', '-f', name], capture_output=True)
        server.shutdown()
        server.server_close()
        asyncio.run(bot._local.aclose())
        asyncio.run(bot._tg.aclose())


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='tls-live-', dir=os.environ.get('TMPDIR')) as temporary:
        main(Path(temporary))
