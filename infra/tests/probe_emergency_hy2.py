"""Real emergency Hysteria2 in private network and mount namespaces, no Internet needed.

The unit steps run as systemd runs them: `vpn2 emergency hy2 open|close|probe` on the
real paths, which overlays keep inside this mount namespace.
"""

import contextlib
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

from probe_hy2_isolation import socks, udp_probe
from probe_io import read_exact
from router_support import ROOT, SB, stop as stop_process
from vpn2.emergency import hy2

UID = 64321  # vpn2-hy2, added to /etc/passwd inside the overlay
NODE, PUBLIC, PRIVATE = '203.0.113.1', '203.0.113.2', '172.16.100.1'
HOME = hy2.STATE / 'hy2'


class Target(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa - HTTPServer callback
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args):  # noqa - HTTPServer callback
        pass


class Echo(socketserver.BaseRequestHandler):
    def handle(self):  # noqa - TCPServer callback
        while data := self.request.recv(4096):
            self.request.sendall(data)


class Datagram(socketserver.BaseRequestHandler):
    def handle(self):  # noqa - UDPServer callback
        data, sock = self.request
        sock.sendto(data, self.client_address)


def serve():
    servers = [http.server.ThreadingHTTPServer(('0.0.0.0', 18080), Target),
               socketserver.ThreadingTCPServer(('0.0.0.0', 18081), Echo),
               socketserver.UDPServer(('0.0.0.0', 18080), Datagram)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        time.sleep(60)


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def vpn2(*args, check=True):
    """The package CLI, as ExecStartPre/ExecStopPost and the probe unit call it."""
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'infra/vpn2/src')}
    result = subprocess.run([sys.executable, '-m', 'vpn2.cli', 'emergency', 'hy2', *args],
                            capture_output=True, text=True, env=env, timeout=60)
    if check and result.returncode:
        raise AssertionError(f'vpn2 emergency hy2 {args[0]}: {result.stderr}')
    return result.returncode


def overlay(work, target):
    """Writable copy of target visible only in this mount namespace."""
    upper, scratch = work / f'{target.name}-upper', work / f'{target.name}-work'
    upper.mkdir()
    scratch.mkdir()
    run(['mount', '-t', 'overlay', 'overlay', '-o',
         f'lowerdir={target},upperdir={upper},workdir={scratch}', str(target)])


def attempt(proxy, host):
    return subprocess.run(['curl', '-fsS', '--max-time', '2', '--noproxy', '',
                           '--socks5-hostname', f'127.0.0.1:{proxy}',
                           f'http://{host}:18080/'], capture_output=True).returncode


def fetch(proxy, host):
    return attempt(proxy, host) == 0


def until(check, what, seconds=5):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, what
        time.sleep(.05)


def refused(proxy, host):
    """Refusal by the server, observed on a client that is already listening.

    curl exits 7 while the client's SOCKS port is not open yet, which says
    nothing about credentials. A ready sing-box client whose tunnel the server
    refused answers SOCKS5 'general failure', curl exit 97.
    """
    deadline = time.monotonic() + 5
    while (code := attempt(proxy, host)) == 7:
        assert time.monotonic() < deadline, f'client on {proxy} never listened'
        time.sleep(.05)
    return code == 97


class Runtime:
    """systemd for the two units: open, sing-box as vpn2-hy2, close; the probe unit."""

    def __init__(self, work, cleanup):
        self.work, self.cleanup, self.process = work, cleanup, None

    def service(self, action):
        if action in ('stop', 'restart'):
            if self.process is not None:
                stop_process(self.process)
                self.process = None
            vpn2('close')
        if action != 'stop' and (self.process is None or self.process.poll() is not None):
            vpn2('open')
            self.launch()

    def launch(self):
        # The unit's group reads the generation (Group=vpn2-emergency).
        for path in (HOME, *HOME.iterdir()):
            os.chown(path, -1, UID)
        config = json.loads((HOME / 'server.json').read_text())
        # Only the DNS transport is replaced, by deterministic hostile answers.
        config['dns']['servers'] = [{'type': 'hosts', 'tag': 'public', 'predefined': {
            'node.test': [NODE], 'private.test': [PRIVATE],
            'loopback.test': ['127.0.0.1'], 'public.test': [PUBLIC],
        }}]
        path = self.work / 'runtime.json'
        path.write_text(json.dumps(config))
        path.chmod(0o644)
        log = self.cleanup.enter_context((self.work / 'server.log').open('a'))
        self.process = subprocess.Popen(['setpriv', f'--reuid={UID}', f'--regid={UID}', '--clear-groups',
                                         SB, 'run', '-c', str(path)], stdout=log, stderr=log)

    def probe(self, _state_dir):
        return vpn2('probe', check=False) == 0


def network(cleanup):
    """Node 203.0.113.1 here; a public 203.0.113.2 and a private 172.16.100.1 next door."""
    run(['ip', 'link', 'set', 'lo', 'up'])
    remote = subprocess.Popen(['unshare', '--net', sys.executable, __file__, '--serve'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cleanup.callback(stop_process, remote)
    until(lambda: os.readlink(f'/proc/{remote.pid}/ns/net') != os.readlink('/proc/self/ns/net'),
          'remote namespace')
    run(['ip', 'link', 'add', 'exit-test', 'type', 'veth', 'peer', 'name', 'internet-test'])
    run(['ip', 'link', 'set', 'internet-test', 'netns', str(remote.pid)])
    run(['ip', 'address', 'add', f'{NODE}/30', 'dev', 'exit-test'])
    run(['ip', 'link', 'set', 'exit-test', 'up'])
    for args in [['link', 'set', 'lo', 'up'], ['link', 'set', 'internet-test', 'up'],
                 ['address', 'add', f'{PUBLIC}/30', 'dev', 'internet-test'],
                 ['address', 'add', f'{PRIVATE}/32', 'dev', 'internet-test']]:
        run(['nsenter', '-t', str(remote.pid), '-n', 'ip', *args])
    run(['ip', 'route', 'add', f'{PRIVATE}/32', 'via', PUBLIC])
    local = subprocess.Popen([sys.executable, __file__, '--serve'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cleanup.callback(stop_process, local)
    # Positive controls: all forbidden destinations really run services.
    for host in ('127.0.0.1', NODE, PRIVATE, PUBLIC):
        until(lambda: subprocess.run(['curl', '-fsS', '--max-time', '1', f'http://{host}:18080/'],
                                     capture_output=True).returncode == 0, f'service at {host}')


def client(work, cleanup, uri, port, server_port=None, certificate=None):
    """A device that imported the profile: server, port, password and SNI from the URI."""
    parts = urlsplit(uri)
    # Each client keeps the certificate from its own issuance.
    cert = work / f'client-{port}.pem'
    cert.write_bytes((certificate or HOME / 'fullchain.pem').read_bytes())
    config = {'log': {'disabled': True}, 'inbounds': [{
        'type': 'socks', 'listen': '127.0.0.1', 'listen_port': port,
    }], 'outbounds': [{
        'type': 'hysteria2', 'server': parts.hostname, 'server_port': server_port or parts.port,
        'password': unquote(parts.username), 'tls': {
            'enabled': True, 'server_name': parse_qs(parts.query)['sni'][0], 'certificate_path': str(cert),
        },
    }]}
    path = work / f'client-{port}.json'
    path.write_text(json.dumps(config))
    process = subprocess.Popen([SB, 'run', '-c', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cleanup.callback(stop_process, process)
    return process


def ordinary(work, cleanup):
    """An independent ordinary Hysteria2 server on the same node, from the same config."""
    config = json.loads((HOME / 'server.json').read_text())
    config.pop('experimental')
    inbound = config['inbounds'][0]
    inbound['listen_port'] = 1443
    for name, key in (('ordinary.pem', 'certificate_path'), ('ordinary.key', 'key_path')):
        (work / name).write_bytes(Path(inbound['tls'][key]).read_bytes())
        inbound['tls'][key] = str(work / name)
    path = work / 'ordinary.json'
    path.write_text(json.dumps(config))
    process = subprocess.Popen([SB, 'run', '-c', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cleanup.callback(stop_process, process)
    return work / 'ordinary.pem'


def statuses():
    hy2.poll_sessions(hy2.STATE)
    return hy2.connections(hy2.STATE, hy2.read_state(HOME)['generation'])


def probe():
    with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as cleanup:
        work = Path(temporary)
        work.chmod(0o755)
        for target in (Path('/etc'), Path('/var/lib')):
            overlay(work, target)
        with open('/etc/passwd', 'a') as passwd:
            passwd.write(f'vpn2-hy2:x:{UID}:{UID}::/nonexistent:/usr/sbin/nologin\n')
        hy2.OPTIONS.parent.mkdir(exist_ok=True)
        vpn2('endpoint', NODE, str(18443), '0')
        network(cleanup)
        runtime = Runtime(work, cleanup)
        cleanup.callback(runtime.service, 'stop')
        options = {'server_ip': NODE, 'binary': SB, 'runtime_dir': str(HOME),
                   'probe_url': f'http://{PUBLIC}:18080/'}

        def start(generation):
            return hy2.start(hy2.STATE, generation, devices, options=options, runtime=runtime)

        def connect(uri, port, *args):
            return client(work, cleanup, uri, port, *args)

        devices = [{'id': 1, 'name': 'one'}, {'id': 2, 'name': 'two'}]
        # The real readiness probe (`vpn2 emergency hy2 probe`) runs inside start.
        first = start(1)
        uri = urlsplit(first[1]['uri'])
        assert (uri.scheme, uri.hostname, uri.port) == ('hysteria2', NODE, 18443), uri
        leaf = (HOME / 'fullchain.pem').read_text().split(hy2.END_CERT)[0] + hy2.END_CERT
        assert parse_qs(uri.query)['pinSHA256'] == [hashlib.sha256(ssl.PEM_cert_to_DER_cert(leaf)).hexdigest()]
        connect(first[1]['uri'], 17890)
        until(lambda: fetch(17890, PUBLIC), 'emergency channel carries no traffic')
        # Started after the emergency control on purpose: its readiness is awaited
        # on its own, not inferred from another tunnel that happened to come up.
        connect(first[1]['uri'], 17893, 1443, ordinary(work, cleanup))
        until(lambda: fetch(17893, PUBLIC), 'ordinary Hysteria2 control failed')
        assert fetch(17890, 'public.test')
        for host in ('127.0.0.1', '127.1.2.3', NODE, PRIVATE, '[::1]', '[::ffff:127.0.0.1]',
                     'node.test', 'private.test', 'loopback.test'):
            assert not fetch(17890, host), f'forbidden destination {host} accessible'
        assert udp_probe(17890, PUBLIC, 18080)['bytes'] > 0
        for host in ('127.0.0.1', NODE, PRIVATE):
            assert udp_probe(17890, host, 18080)['bytes'] == 0, host
        # nft isolation holds on its own, without sing-box's private-IP route rule.
        for host, expected in [(PUBLIC, True), (NODE, False), (PRIVATE, False), ('127.0.0.1', False)]:
            result = subprocess.run(['setpriv', f'--reuid={UID}', f'--regid={UID}', '--clear-groups',
                                     'curl', '-fsS', '--noproxy', '*', '--max-time', '1',
                                     f'http://{host}:18080/'], capture_output=True)
            assert (result.returncode == 0) == expected, host
        print('isolation: public TCP/UDP pass; node, loopback, private and DNS aliases blocked; '
              'nft independently enforced')
        connection = cleanup.enter_context(socks(17890, 18081, target_host=PUBLIC))
        connection.sendall(b'before')
        assert read_exact(connection, 6, 'TCP echo before restart') == b'before'
        before = (HOME / 'server.json').read_bytes()
        # An agent restart resumes the same generation: same profiles, live streams kept.
        assert start(1) == first
        assert (HOME / 'server.json').read_bytes() == before
        connection.sendall(b'resume')
        assert read_exact(connection, 6, 'TCP echo after idempotent start') == b'resume'
        hy2.stop(hy2.STATE, runtime=runtime)
        assert fetch(17893, PUBLIC)
        assert not fetch(17890, PUBLIC)
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            connection.sendall(b'after')
            assert connection.recv(5) == b''
        # A stopped generation is never resurrected: issuing it again mints new passwords.
        assert start(1)[1]['uri'] != first[1]['uri'], 'revoked credentials restored'
        second = start(2)
        # The current certificate with the OLD password isolates auth from TLS.
        connect(first[1]['uri'], 17891)
        connect(second[1]['uri'], 17892)
        until(lambda: fetch(17892, PUBLIC), 'new credentials rejected')
        assert refused(17891, PUBLIC), 'old password accepted'
        # A QUIC session from before the restart is unknown to the new process:
        # sing-box neither closes it on SIGTERM nor resets it, so the old client
        # stalls until its idle timeout (~30 s) unless it happened to send while
        # the port was closed. Credentials are checked with sessions opened
        # after the restart.
        runtime.service('restart')
        assert hy2.probe(hy2.STATE)
        assert fetch(17893, PUBLIC)
        connect(first[1]['uri'], 17896)
        current = connect(second[1]['uri'], 17897)
        until(lambda: fetch(17897, PUBLIC), 'new credentials rejected after restart')
        assert refused(17896, PUBLIC), 'old password accepted after restart'
        # Session source: the instance's own Clash API through the real firewall.
        with socks(17897, 18081, target_host=PUBLIC) as stream:
            stream.sendall(b'live')
            assert read_exact(stream, 4, 'session echo, first device') == b'live'
            assert statuses() == {1: 'connected', 2: 'never'}, statuses()
        stop_process(current)
        until(lambda: statuses() == {1: 'seen', 2: 'never'}, 'disconnected device not seen')
        runtime.process.terminate()
        runtime.process.wait()
        assert statuses() == {}
        third = start(3)
        assert statuses() == {1: 'never', 2: 'never'}
        connect(third[2]['uri'], 17898)
        until(lambda: fetch(17898, PUBLIC), 'third generation carries no traffic')
        with socks(17898, 18081, target_host=PUBLIC) as stream:
            stream.sendall(b'live')
            assert read_exact(stream, 4, 'session echo, new generation') == b'live'
            assert statuses() == {1: 'never', 2: 'connected'}, statuses()
        print('sessions: connected -> seen after disconnect -> unknown without source -> never in a new generation')
        hy2.stop(hy2.STATE, runtime=runtime)
        assert not fetch(17898, PUBLIC)
        assert fetch(17893, PUBLIC)
        # An unrelated node service and the ordinary user's traffic remain alive.
        with socket.create_connection(('127.0.0.1', 18080), timeout=1):
            pass
        print('old credentials rejected after restart; new credentials work; active sessions closed; '
              'ordinary traffic unaffected')


if __name__ == '__main__':
    if sys.argv[1:] == ['--serve']:
        serve()
    else:
        probe()
