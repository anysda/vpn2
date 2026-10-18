"""Real emergency instance in a private network namespace, no Internet needed."""

import contextlib
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

from probe_hy2_isolation import socks, udp_probe
from router_support import SB, stop as stop_process
from test_emergency_hy2 import module


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


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def fetch(proxy, host):
    return subprocess.run(['curl', '-fsS', '--max-time', '2', '--noproxy', '',
                           '--socks5-hostname', f'127.0.0.1:{proxy}',
                           f'http://{host}:18080/'], capture_output=True).returncode == 0


def probe():
    hy2 = module()
    with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as cleanup:
        directory = Path(temporary)
        directory.chmod(0o755)
        run(['ip', 'link', 'set', 'lo', 'up'])
        remote = subprocess.Popen(['unshare', '--net', sys.executable, __file__, '--serve'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cleanup.callback(stop_process, remote)
        deadline = time.monotonic() + 5
        while os.readlink(f'/proc/{remote.pid}/ns/net') == os.readlink('/proc/self/ns/net'):
            assert time.monotonic() < deadline
            time.sleep(.01)
        run(['ip', 'link', 'add', 'exit-test', 'type', 'veth', 'peer', 'name', 'internet-test'])
        run(['ip', 'link', 'set', 'internet-test', 'netns', str(remote.pid)])
        run(['ip', 'address', 'add', '203.0.113.1/30', 'dev', 'exit-test'])
        run(['ip', 'link', 'set', 'exit-test', 'up'])
        for args in [['link', 'set', 'lo', 'up'], ['link', 'set', 'internet-test', 'up'],
                     ['address', 'add', '203.0.113.2/30', 'dev', 'internet-test'],
                     ['address', 'add', '172.16.100.1/32', 'dev', 'internet-test']]:
            run(['nsenter', '-t', str(remote.pid), '-n', 'ip', *args])
        run(['ip', 'route', 'add', '172.16.100.1/32', 'via', '203.0.113.2'])
        local = subprocess.Popen([sys.executable, __file__, '--serve'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cleanup.callback(stop_process, local)
        options = {'server_ip': '203.0.113.1', 'binary': SB, 'tls_dir': str(directory / 'missing'),
                   'runtime_dir': str(directory / 'hy2'), 'probe_url': 'http://203.0.113.2:18080/'}

        class Runtime:
            process = None
            probe = staticmethod(hy2.probe)

            def ensure_started(self):
                if self.process is None or self.process.poll() is not None:
                    self.restart()

            def restart(self):
                self.stop()
                hy2.validate(directory / 'hy2')
                for path in (directory / 'hy2').iterdir():
                    if path.is_file():
                        os.chown(path, -1, 65534)
                os.chown(directory / 'hy2', -1, 65534)
                rules = hy2.firewall_rules(65534, 18443, True)
                subprocess.run(['nft', '-f', '-'], input='delete table inet vpn2_emergency_hy2\n' + rules,
                               text=True, check=True, capture_output=True)
                config = json.loads((directory / 'hy2/server.json').read_text())
                # Replace only DNS transport with deterministic hostile answers.
                config['dns']['servers'] = [{'type': 'hosts', 'tag': 'public', 'predefined': {
                    'node.test': ['203.0.113.1'], 'private.test': ['172.16.100.1'],
                    'loopback.test': ['127.0.0.1'], 'public.test': ['203.0.113.2'],
                }}]
                path = directory / 'runtime.json'
                path.write_text(json.dumps(config))
                path.chmod(0o644)
                log = cleanup.enter_context((directory / 'server.log').open('a'))
                self.process = subprocess.Popen(['setpriv', '--reuid=65534', '--regid=65534',
                                                 '--clear-groups', SB, 'run', '-c', str(path)],
                                                stdout=log, stderr=log)

            def stop(self):
                if self.process is not None:
                    stop_process(self.process)
                    self.process = None
                exists = subprocess.run(['nft', 'list', 'table', 'inet', 'vpn2_emergency_hy2'],
                                        capture_output=True).returncode == 0
                prefix = 'delete table inet vpn2_emergency_hy2\n' if exists else ''
                subprocess.run(['nft', '-f', '-'], text=True, check=True, capture_output=True,
                               input=prefix + hy2.firewall_rules(65534, 18443, False))

        runtime = Runtime()
        cleanup.callback(runtime.stop)
        # Positive controls: all forbidden destinations really run services.
        for host in ('127.0.0.1', '203.0.113.1', '172.16.100.1', '203.0.113.2'):
            deadline = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection((host, 18080), timeout=.2):
                        break
                except OSError:
                    assert time.monotonic() < deadline
                    time.sleep(.05)

        def client(uri, port, server_port=18443, certificate=None):
            parts = urlsplit(uri)
            config = {'log': {'disabled': True}, 'inbounds': [{
                'type': 'socks', 'listen': '127.0.0.1', 'listen_port': port,
            }], 'outbounds': [{
                'type': 'hysteria2', 'server': '127.0.0.1', 'server_port': server_port,
                'password': unquote(parts.username), 'tls': {
                    'enabled': True, 'server_name': '203.0.113.1',
                    'certificate_path': str(directory / 'hy2/fullchain.pem'),
                },
            }]}
            # Each client retains the certificate from its original issuance.
            cert = directory / f'client-{port}.pem'
            cert.write_bytes((certificate or directory / 'hy2/fullchain.pem').read_bytes())
            config['outbounds'][0]['tls']['certificate_path'] = str(cert)
            path = directory / f'client-{port}.json'
            path.write_text(json.dumps(config))
            process = subprocess.Popen([SB, 'run', '-c', str(path)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cleanup.callback(stop_process, process)
            return process

        devices = [{'id': 1, 'name': 'one'}, {'id': 2, 'name': 'two'}]
        first = hy2.start(directory, 1, devices, options=options, runtime=runtime)
        # Keep an independent ordinary Hysteria2 tunnel alive during all actions.
        ordinary_cert = directory / 'ordinary.pem'
        ordinary_cert.write_bytes((directory / 'hy2/fullchain.pem').read_bytes())
        ordinary = json.loads((directory / 'hy2/server.json').read_text())
        ordinary['inbounds'][0]['listen_port'] = 1443
        ordinary['inbounds'][0]['tls']['certificate_path'] = str(ordinary_cert)
        ordinary_key = directory / 'ordinary.key'
        ordinary_key.write_bytes((directory / 'hy2/privkey.pem').read_bytes())
        ordinary['inbounds'][0]['tls']['key_path'] = str(ordinary_key)
        ordinary_path = directory / 'ordinary.json'
        ordinary_path.write_text(json.dumps(ordinary))
        ordinary_process = subprocess.Popen([SB, 'run', '-c', str(ordinary_path)],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cleanup.callback(stop_process, ordinary_process)
        client(first[1]['uri'], 17893, 1443, ordinary_cert)
        client(first[1]['uri'], 17890)
        deadline = time.monotonic() + 5
        while not fetch(17890, '203.0.113.2'):
            assert time.monotonic() < deadline, 'positive QUIC control failed'
        assert fetch(17893, '203.0.113.2')
        native = os.environ.get('HYSTERIA_BIN')
        if native:
            uri = urlsplit(first[1]['uri'])
            pin = parse_qs(uri.query)['pinSHA256'][0]
            for fingerprint, port in ((pin, 17894), ('00' * 32, 17895)):
                path = directory / f'native-{port}.json'
                path.write_text(json.dumps({
                    'server': '127.0.0.1:18443', 'auth': unquote(uri.username),
                    'tls': {'sni': '203.0.113.1', 'insecure': True, 'pinSHA256': fingerprint},
                    'socks5': {'listen': f'127.0.0.1:{port}'},
                }))
                process = subprocess.Popen([native, '-c', str(path), 'client'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                cleanup.callback(stop_process, process)
                if fingerprint == pin:
                    deadline = time.monotonic() + 5
                    while not fetch(port, '203.0.113.2'):
                        assert time.monotonic() < deadline, 'native pinned client failed'
                else:
                    assert process.wait(timeout=5) != 0
                    assert not fetch(port, '203.0.113.2')
            print('native Hysteria: URI pinSHA256 accepted; wrong certificate pin rejected')
        assert fetch(17890, 'public.test')
        for host in ('127.0.0.1', '127.1.2.3', '203.0.113.1', '172.16.100.1',
                     '[::1]', '[::ffff:127.0.0.1]', 'node.test', 'private.test', 'loopback.test'):
            assert not fetch(17890, host), 'forbidden destination accessible'
        assert udp_probe(17890, '203.0.113.2', 18080)['bytes'] > 0
        for host in ('127.0.0.1', '203.0.113.1', '172.16.100.1'):
            assert udp_probe(17890, host, 18080)['bytes'] == 0
        # Prove nft isolation independently of sing-box's private-IP route rule.
        for host, expected in [('203.0.113.2', True), ('203.0.113.1', False),
                               ('172.16.100.1', False), ('127.0.0.1', False)]:
            result = subprocess.run(['setpriv', '--reuid=65534', '--regid=65534', '--clear-groups',
                                     'curl', '-fsS', '--noproxy', '*', '--max-time', '1',
                                     f'http://{host}:18080/'], capture_output=True)
            assert (result.returncode == 0) == expected
        print('isolation: public TCP/UDP pass; node, loopback, private and DNS aliases blocked; nft independently enforced')
        connection = cleanup.enter_context(socks(17890, 18081, target_host='203.0.113.2'))
        connection.sendall(b'before')
        assert connection.recv(6) == b'before'
        before = hashlib.sha256((directory / 'hy2/server.json').read_bytes()).hexdigest()
        assert hy2.start(directory, 1, devices, options=options, runtime=runtime) == first
        assert before == hashlib.sha256((directory / 'hy2/server.json').read_bytes()).hexdigest()
        connection.sendall(b'resume')
        assert connection.recv(6) == b'resume'
        hy2.stop(directory, runtime=runtime)
        assert fetch(17893, '203.0.113.2')
        assert not fetch(17890, '203.0.113.2')
        try:
            connection.sendall(b'after')
            assert connection.recv(5) == b''
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            hy2.start(directory, 1, devices, options=options, runtime=runtime)
        except ValueError:
            pass
        else:
            raise AssertionError('revoked generation restored')
        second = hy2.start(directory, 2, devices, options=options, runtime=runtime)
        # Use the new certificate with the OLD password to isolate auth rejection
        # from certificate rotation: stale credentials must fail even with valid TLS.
        client(first[1]['uri'], 17891)
        client(second[1]['uri'], 17892)
        deadline = time.monotonic() + 5
        while not fetch(17892, '203.0.113.2'):
            assert time.monotonic() < deadline
        assert not fetch(17891, '203.0.113.2')
        runtime.restart()
        assert hy2.selftest(directory)
        assert fetch(17893, '203.0.113.2')
        assert not fetch(17891, '203.0.113.2')
        assert fetch(17892, '203.0.113.2')
        hy2.stop(directory, runtime=runtime)
        assert not fetch(17892, '203.0.113.2')
        assert fetch(17893, '203.0.113.2')
        # An unrelated node service and the ordinary user's traffic remain alive.
        with socket.create_connection(('127.0.0.1', 18080), timeout=1):
            pass
        print('old credentials rejected after restart; new credentials work; active sessions closed; ordinary traffic unaffected')


if __name__ == '__main__':
    if sys.argv[1:] == ['--serve']:
        serve()
    else:
        probe()
