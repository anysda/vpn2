"""Real Hysteria2 loopback isolation probe, run in a private network namespace."""

import contextlib
import http.server
import ipaddress
import json
from pathlib import Path
import socket
import socketserver
from string import Template
import struct
import subprocess
import tempfile
import threading
import time

from router_support import ROOT, SB


class Target(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):  # noqa - called by BaseHTTPRequestHandler
        Target.hits += 1
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):  # noqa - called by BaseHTTPRequestHandler
        pass


class Target6(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6  # noqa - read by TCPServer.__init__


class UdpTarget(socketserver.BaseRequestHandler):
    hits = 0

    def handle(self):  # noqa - called by UDPServer
        UdpTarget.hits += 1
        data, sock = self.request
        sock.sendto(data, self.client_address)


def address(host, port):
    try:
        ip = ipaddress.ip_address(host)
        encoded = (b'\x01' if ip.version == 4 else b'\x04') + ip.packed
    except ValueError:
        encoded = b'\x03' + bytes([len(host)]) + host.encode('ascii')
    return encoded + struct.pack('!H', port)


def tcp_request(proxy, host, port):
    try:
        with socket.create_connection(('127.0.0.1', proxy), timeout=2) as sock:
            sock.sendall(b'\x05\x01\x00')
            assert sock.recv(2) == b'\x05\x00'
            sock.sendall(b'\x05\x01\x00' + address(host, port))
            reply = sock.recv(64)
            if len(reply) < 2 or reply[1] != 0:
                return False
            sock.sendall(b'GET /metrics HTTP/1.0\r\nHost: fixture.test\r\n\r\n')
            return sock.recv(4096).startswith(b'HTTP/')
    except OSError:
        return False


def udp_request(host):
    with socket.create_connection(('127.0.0.1', 17897), timeout=2) as control:
        control.sendall(b'\x05\x01\x00')
        assert control.recv(2) == b'\x05\x00'
        control.sendall(b'\x05\x03\x00\x01' + b'\x00' * 6)
        reply = control.recv(64)
        assert reply[:4] == b'\x05\x00\x00\x01', reply
        relay = socket.inet_ntoa(reply[4:8]), struct.unpack('!H', reply[8:10])[0]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(.5)
            sock.sendto(b'\x00\x00\x00' + address(host, 9100) + b'probe', relay)
            try:
                return sock.recv(4096).endswith(b'probe')
            except OSError:
                return False


with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as cleanup:
    directory = Path(tmp)
    subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)
    subprocess.run(['ip', 'address', 'add', '192.0.2.20/32', 'dev', 'lo'], check=True)
    for server in [http.server.ThreadingHTTPServer(('0.0.0.0', port), Target)
                   for port in (22, 9100, 19091)] + [
                       Target6(('::1', 9100), Target),
                       socketserver.UDPServer(('0.0.0.0', 9100), UdpTarget)]:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        cleanup.callback(server.server_close)
        cleanup.callback(thread.join)
        cleanup.callback(server.shutdown)
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                    'ec_paramgen_curve:P-256', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost',
                    '-keyout', str(directory / 'tls.key'), '-out', str(directory / 'tls.crt')],
                   check=True, capture_output=True)
    env = {'HY2_DIRECT_PORT': '1443', 'HY2_WARP_PORT': '2443', 'HY2_MGMT_PORT': '3443',
           'WGCF_LOCAL_IPV4': '192.0.2.10', 'WGCF_PEER_ENDPOINT_HOST': '127.0.0.1',
           'WGCF_PEER_ENDPOINT_PORT': '2408', 'WGCF_RESERVED_JSON': '[0,0,0]',
           'WGCF_PRIVKEY': 'wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0=',
           'WGCF_PEER_PUBKEY': 'wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0='}
    for key in ('HY2_PWD_DIRECT', 'HY2_PWD_WARP', 'HY2_PWD_MGMT', 'HY2_OBFS_PWD', 'CLASH_SECRET'):
        env[key] = key + '-test-password'
    template = (ROOT / 'infra/configs/sing-box-server.json.tpl').read_text()
    cfg = json.loads(Template(template).substitute(env).replace('/etc/sing-box', str(directory)))
    # Controlled DNS answers include an arbitrary name and a mixed public/local answer.
    cfg['dns'] = {'servers': [{'type': 'hosts', 'tag': 'fixture', 'predefined': {
        'loopback.test': ['127.0.0.1'], 'loopback6.test': ['::1'],
        'mixed.test': ['192.0.2.20', '127.0.0.1'], 'public.test': ['192.0.2.20'],
    }}]}
    client = {'log': {'level': 'error'}, 'inbounds': [], 'outbounds': [], 'route': {'rules': []}}
    for lane, proxy, remote in [('DIRECT', 17897, 1443), ('MGMT', 17898, 3443)]:
        client['inbounds'].append({'type': 'socks', 'tag': lane, 'listen': '127.0.0.1',
                                   'listen_port': proxy})
        client['outbounds'].append({
            'type': 'hysteria2', 'tag': lane, 'server': '127.0.0.1', 'server_port': remote,
            'password': env['HY2_PWD_' + lane],
            'obfs': {'type': 'salamander', 'password': env['HY2_OBFS_PWD']},
            'tls': {'enabled': True, 'server_name': 'localhost',
                    'certificate_path': str(directory / 'tls.crt')},
        })
        client['route']['rules'].append({'inbound': lane, 'outbound': lane})
    processes = []
    for name, config in [('exit', cfg), ('client', client)]:
        path = directory / (name + '.json')
        path.write_text(json.dumps(config))
        subprocess.run([SB, 'check', '-c', str(path)], check=True)
        log = cleanup.enter_context(open(directory / (name + '.log'), 'w+'))
        process = subprocess.Popen([SB, 'run', '-c', str(path)], stdout=log, stderr=log)
        processes.append(process)
        cleanup.callback(process.wait, 5)
        cleanup.callback(process.terminate)
    for _ in range(100):
        try:
            with socket.create_connection(('127.0.0.1', 17898), timeout=.1):
                break
        except OSError:
            time.sleep(.05)
    assert all(p.poll() is None for p in processes), 'sing-box failed to start'
    assert tcp_request(17897, '192.0.2.20', 9100), 'public TCP must work'
    assert udp_request('192.0.2.20'), 'public UDP must work'
    failures = []
    for host, port in [('127.0.0.1', 9100), ('127.0.0.1', 22), ('127.0.0.1', 9090),
                       ('127.1.2.3', 9100), ('::1', 9100), ('::ffff:127.0.0.1', 9100),
                       ('0.0.0.0', 9100), ('::', 9100), ('localhost', 9100),
                       ('loopback.test', 9100), ('loopback6.test', 9100), ('mixed.test', 9100)]:
        before = Target.hits
        reached = tcp_request(17897, host, port)
        if reached or Target.hits != before:
            failures.append(f'TCP {host}:{port}')
    for host in ('127.0.0.1', 'loopback.test', 'mixed.test'):
        before = UdpTarget.hits
        if udp_request(host) or UdpTarget.hits != before:
            failures.append(f'UDP {host}:9100')
    assert not failures, 'loopback reachable through user tunnel: ' + ', '.join(failures)
    assert tcp_request(17897, 'public.test', 9100), 'public DNS destination must work'
    assert udp_request('public.test'), 'public DNS UDP destination must work'
    print('direct: loopback blocked; public TCP/UDP reachable', flush=True)
    for port in (22, 9100):
        assert tcp_request(17898, '127.0.0.1', port), f'mgmt port {port} must work'
    for host, port in [('127.0.0.1', 19091), ('192.0.2.20', 9100)]:
        before = Target.hits
        assert not tcp_request(17898, host, port) and Target.hits == before
    print('mgmt: ports 22/9100 reachable; other destinations blocked', flush=True)
