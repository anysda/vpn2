"""Live TCP/UDP fail-closed probe; invoked only inside a private network namespace."""

import contextlib
import http.server
import json
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
from string import Template

from router_support import ROOT, SB, generate


def wait_port(port):
    for _ in range(100):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.1):
                return
        except OSError:
            time.sleep(.05)
    raise RuntimeError(f'listener {port} did not start')


def request():
    with socket.create_connection(('127.0.0.1', 17897), timeout=6) as sock:
        sock.settimeout(6)
        sock.sendall(b'\x05\x01\x00')
        assert sock.recv(2) == b'\x05\x00'
        sock.sendall(b'\x05\x01\x00\x01' + socket.inet_aton('8.8.8.8') + struct.pack('!H', 18080))
        reply = sock.recv(64)
        if len(reply) < 2 or reply[1] != 0:
            return False
        sock.sendall(b'GET / HTTP/1.0\r\nHost: example.com\r\n\r\n')
        return b'200 OK' in sock.recv(4096)


def request_udp():
    with socket.create_connection(('127.0.0.1', 17897), timeout=3) as control:
        control.sendall(b'\x05\x01\x00')
        assert control.recv(2) == b'\x05\x00'
        control.sendall(b'\x05\x03\x00\x01' + b'\x00' * 6)
        reply = control.recv(64)
        assert reply[:4] == b'\x05\x00\x00\x01', reply
        relay = socket.inet_ntoa(reply[4:8]), struct.unpack('!H', reply[8:10])[0]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(3)
            packet = b'\x00\x00\x00\x01' + socket.inet_aton('8.8.8.8') + struct.pack('!H', 18081) + b'udp-probe'
            sock.sendto(packet, relay)
            return sock.recv(4096).endswith(b'udp-probe')


class Target(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):  # noqa - called by BaseHTTPRequestHandler
        Target.hits += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'target reached')

    def log_message(self, *_args):  # noqa - called by BaseHTTPRequestHandler
        pass


class UdpTarget(socketserver.BaseRequestHandler):
    hits = 0

    def handle(self):  # noqa - called by UDPServer
        UdpTarget.hits += 1
        data, sock = self.request
        sock.sendto(data, self.client_address)


with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as cleanup:
    directory = Path(tmp)
    subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)
    subprocess.run(['ip', 'address', 'add', '8.8.8.8/32', 'dev', 'lo'], check=True)
    server = http.server.ThreadingHTTPServer(('8.8.8.8', 18080), Target)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    cleanup.callback(server.server_close)
    cleanup.callback(thread.join)
    cleanup.callback(server.shutdown)
    udp = socketserver.UDPServer(('8.8.8.8', 18081), UdpTarget)
    udp_thread = threading.Thread(target=udp.serve_forever)
    udp_thread.start()
    cleanup.callback(udp.server_close)
    cleanup.callback(udp_thread.join)
    cleanup.callback(udp.shutdown)
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                    'ec_paramgen_curve:P-256', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                    '-keyout', str(directory / 'tls.key'), '-out', str(directory / 'tls.crt')],
                   check=True, capture_output=True)
    fixture = {'HY2_DIRECT_PORT': '1443', 'HY2_WARP_PORT': '2443', 'HY2_MGMT_PORT': '3443',
               'WGCF_LOCAL_IPV4': '192.0.2.10', 'WGCF_PEER_ENDPOINT_HOST': '127.0.0.1',
               'WGCF_PEER_ENDPOINT_PORT': '2408', 'WGCF_RESERVED_JSON': '[0,0,0]',
               'WGCF_PRIVKEY': 'wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0=',
               'WGCF_PEER_PUBKEY': 'wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0='}
    for key in ('HY2_PWD_DIRECT', 'HY2_PWD_WARP', 'HY2_PWD_MGMT', 'HY2_OBFS_PWD', 'CLASH_SECRET'):
        fixture[key] = 'test-password-0123456789'
    template = (ROOT / 'infra/configs/sing-box-server.json.tpl').read_text()
    exit_path = directory / 'exit.json'
    exit_path.write_text(Template(template).substitute(fixture).replace('/etc/sing-box', str(directory)))
    cfg = json.loads(generate(EXIT_TAGS='aa', DOMAIN_AA='127.0.0.1'))
    cfg['experimental']['clash_api']['external_controller'] = '127.0.0.1:19090'
    cfg['experimental']['cache_file']['path'] = str(directory / 'cache.db')
    for inbound in cfg['inbounds']:
        # SOCKS supplies destinations without needing a second client namespace.
        # The generated routing rules and Hysteria2 outbounds stay unchanged.
        if inbound['tag'] == 'wg-tproxy-in':
            inbound.update(type='mixed', listen_port=17897)
    for rule in cfg['route']['rule_set']:
        path = directory / (rule['tag'] + '.json')
        path.write_text('{"version":2,"rules":[]}')
        rule.update(format='source', path=str(path))
    router_path = directory / 'router.json'
    router_path.write_text(json.dumps(cfg))
    for path in (exit_path, router_path):
        subprocess.run([SB, 'check', '-c', str(path)], check=True)

    def start(path):
        log = cleanup.enter_context(open(directory / (path.stem + '.log'), 'w+'))
        proc = subprocess.Popen([SB, 'run', '-c', str(path)], stdout=log, stderr=log)
        cleanup.callback(proc.wait, 5)
        cleanup.callback(proc.terminate)
        return proc

    exit_proc = start(exit_path)
    start(router_path)
    wait_port(17897)
    time.sleep(.5)
    assert request(), 'healthy tunnel did not deliver request'
    print(f'healthy exit: HTTP 200 via generated Hysteria2 config; target_hits={Target.hits}', flush=True)
    assert request_udp()
    print(f'healthy exit: UDP echo via generated Hysteria2 config; target_hits={UdpTarget.hits}', flush=True)
    exit_proc.terminate()
    exit_proc.wait(5)
    before = Target.hits
    try:
        leaked = request()
    except (OSError, TimeoutError):
        leaked = False
    assert not leaked and Target.hits == before
    print(f'exit down: request failed; target_hits={Target.hits}; direct fallback not observed', flush=True)
    before = UdpTarget.hits
    try:
        leaked = request_udp()
    except (OSError, TimeoutError):
        leaked = False
    assert not leaked and UdpTarget.hits == before
    print(f'exit down: UDP failed; target_hits={UdpTarget.hits}; direct fallback not observed', flush=True)
