#!/usr/bin/env python3
"""Real QUIC regression against an unmodified sing-box binary.

python3 infra/tests/probe_hy2_isolation.py --router-binary /path/to/router \
    --server-binary /path/to/stock-sing-box

Eight TCP receivers stop reading while a ninth downloads 10 MiB through the
same outbound. Exit 1 on starvation; --variant stock reproduces 1.12.25.
--variant generated tests the repository's actual hub and exit configs;
use a non-loopback --origin-address (the exit deliberately blocks loopback).
--server-ssh and --server-address move the exit to an isolated systemd host;
--ssh-key supplies its temporary identity. --sigstop-after 6 suspends a real
receiver process after six seconds of reading. --benchmark-rounds 3 measures
three batches of eight simultaneous 10 MiB downloads before freezing.
"""
import argparse
import concurrent.futures
import contextlib
import copy
import http.server
import json
import multiprocessing
import os
from pathlib import Path
import queue
import re
import shlex
import signal
import socket
import socketserver
import ssl
from string import Template
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import urllib.error


SIZE = 10 * 1024 * 1024
BLOCK = b'x' * 65536


def read_exact(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        data = sock.recv(size - len(chunks))
        if not data:
            raise EOFError('truncated stream')
        chunks.extend(data)
    return bytes(chunks)


class Origin(socketserver.BaseRequestHandler):
    def handle(self):  # noqa - called by TCPServer
        try:
            # Server-first traffic also exercises the wrapped EarlyConn.
            self.request.sendall(b'hello')
            kind = self.request.recv(1)
            if kind == b'S':
                # Keep sending beyond the shared QUIC window, including when
                # a real reader runs for several seconds before SIGSTOP.
                while True:
                    self.request.sendall(BLOCK)
            elif kind == b'P':
                for _ in range(SIZE // len(BLOCK)):
                    self.request.sendall(BLOCK)
            elif kind == b'U':
                received = 0
                try:
                    while data := self.request.recv(65536):
                        received += len(data)
                        # QUIC ACK is not proof the target application read it.
                        time.sleep(.002)
                finally:
                    self.server.uploads.put(received)
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True  # noqa - read by ThreadingMixIn


class DelayTarget(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):  # noqa - called by BaseHTTPRequestHandler
        # Clash treats a sub-millisecond local result (delay=0) as failure.
        time.sleep(.01)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args):  # noqa - called by BaseHTTPRequestHandler
        pass


class UdpTarget(socketserver.BaseRequestHandler):
    def handle(self):  # noqa - called by UDPServer
        data, sock = self.request
        sock.sendto(data, self.client_address)


def udp_probe(proxy_port, target_host, target_port):
    try:
        with socket.create_connection(('127.0.0.1', proxy_port), timeout=3) as control:
            control.sendall(b'\x05\x01\x00')
            if read_exact(control, 2) != b'\x05\x00':
                raise RuntimeError('SOCKS auth failed')
            control.sendall(b'\x05\x03\x00\x01' + b'\x00' * 6)
            reply = read_exact(control, 10)
            if reply[:4] != b'\x05\x00\x00\x01':
                raise RuntimeError('SOCKS UDP failed')
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(3)
                payload = b'udp-probe' * 128
                sock.sendto(b'\x00\x00\x00\x01' + socket.inet_aton(target_host)
                            + struct.pack('!H', target_port) + payload,
                            (socket.inet_ntoa(reply[4:8]), struct.unpack('!H', reply[8:])[0]))
                return {'bytes': len(payload) if sock.recv(4096).endswith(payload) else 0}
    except (OSError, EOFError, RuntimeError) as error:
        return {'bytes': 0, 'error': str(error)}


def port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def socks(proxy_port, target_port, small=False, target_host='127.0.0.1'):
    sock = socket.socket()
    try:
        if small:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.settimeout(5)
        sock.connect(('127.0.0.1', proxy_port))
        sock.sendall(b'\x05\x01\x00')
        if read_exact(sock, 2) != b'\x05\x00':
            raise RuntimeError('SOCKS auth failed')
        sock.sendall(b'\x05\x01\x00\x01' + socket.inet_aton(target_host)
                     + struct.pack('!H', target_port))
        header = read_exact(sock, 4)
        if header[:2] != b'\x05\x00':
            raise RuntimeError(f'SOCKS connect failed: {header!r}')
        read_exact(sock, (4 if header[3] == 1 else 16) + 2)
        return sock
    except BaseException:
        sock.close()
        raise


def download(proxy_port, target_port, target_host='127.0.0.1', timeout=5):
    started = time.monotonic()
    received = 0
    try:
        with socks(proxy_port, target_port, target_host=target_host) as sock:
            if read_exact(sock, 5) != b'hello':
                raise EOFError('missing server-first banner')
            sock.sendall(b'P')
            while received < SIZE:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError('download exceeded deadline')
                sock.settimeout(remaining)
                data = sock.recv(65536)
                if not data:
                    break
                received += len(data)
        return {'bytes': received, 'seconds': round(time.monotonic() - started, 3)}
    except (OSError, EOFError, RuntimeError) as error:
        return {'bytes': received, 'seconds': round(time.monotonic() - started, 3),
                'error': str(error)}


def receiving_client(proxy_port, target_port, target_host, streams, ready):
    """A real reader process that the parent can suspend with SIGSTOP."""
    barrier = threading.Barrier(streams + 1)

    def receive():
        with socks(proxy_port, target_port, target_host=target_host) as sock:
            read_exact(sock, 5)
            sock.sendall(b'S')
            barrier.wait()
            sock.settimeout(None)
            while sock.recv(65536):
                pass

    threads = [threading.Thread(target=receive) for _ in range(streams)]
    for thread in threads:
        thread.start()
    barrier.wait()
    ready.send(True)
    ready.close()
    for thread in threads:
        thread.join()


def configure_variant(router, server, variant):
    """Change only the transport or dispatch policy under investigation."""
    outbound = router['outbounds'][0]
    inbound = server['inbounds'][0]
    frozen_port = router['inbounds'][0]['listen_port']
    if variant.startswith('split-') or variant == 'urltest':
        other = copy.deepcopy(outbound)
        other['tag'] = 'hy2-other-direct'
        if variant == 'split-port':
            other_inbound = copy.deepcopy(inbound)
            other_inbound['listen_port'] = port(socket.SOCK_DGRAM)
            other['server_port'] = other_inbound['listen_port']
            server['inbounds'].append(other_inbound)
        elif variant == 'split-user':
            other['password'] = 'another-test-password'
            inbound['users'].append({'password': other['password']})
        router['outbounds'].append(other)
        if variant == 'urltest':
            router['outbounds'].append({
                'type': 'urltest', 'tag': 'pool',
                'outbounds': [outbound['tag'], other['tag']],
                'url': 'http://127.0.0.1:1/', 'interval': '1h', 'idle_timeout': '1h',
            })
            router['route'] = {'final': 'pool'}
        else:
            # Model two source clients with distinct routing identities. The
            # original outbound remains assigned to the frozen client only.
            frozen_port = port()
            router['inbounds'][0]['tag'] = 'reader'
            router['inbounds'].append({
                'type': 'socks', 'tag': 'frozen', 'listen': '127.0.0.1',
                'listen_port': frozen_port,
            })
            router['route'] = {'rules': [
                {'inbound': 'reader', 'outbound': other['tag']},
                {'inbound': 'frozen', 'outbound': outbound['tag']},
            ]}
    elif variant == 'tuic':
        uuid = '00000000-0000-4000-8000-000000000001'
        inbound['type'] = outbound['type'] = 'tuic'
        inbound['users'][0]['uuid'] = outbound['uuid'] = uuid
    elif variant in ('hy2-yamux', 'hy2-trojan-yamux', 'hy2-trojan-h2mux', 'hy2-trojan-h2mux-one'):
        uuid = '00000000-0000-4000-8000-000000000001'
        inner_port = port()
        outbound['tag'] = 'hy2-carrier'
        outbound['obfs'] = inbound['obfs'] = {
            'type': 'salamander', 'password': 'test-salamander-password',
        }
        server['inbounds'].append({
            'type': 'vless', 'listen': '127.0.0.1', 'listen_port': inner_port,
            'users': [{'uuid': uuid}], 'multiplex': {'enabled': True},
        })
        router['outbounds'].insert(0, {
            'type': 'vless', 'tag': 'hy2-test-direct', 'server': '127.0.0.1',
            'server_port': inner_port, 'uuid': uuid, 'detour': 'hy2-carrier',
            'multiplex': {'enabled': True, 'protocol': 'yamux', 'max_connections': 1},
        })
        if variant.startswith('hy2-trojan-'):
            server['inbounds'][-1].update(type='trojan', users=[{'password': 'inner-test-password'}])
            inner = router['outbounds'][0]
            inner.update(type='trojan', password='inner-test-password')
            del inner['uuid']
            if 'h2mux' in variant:
                inner['multiplex'].update(protocol='h2mux', max_connections=1 if variant.endswith('-one') else 4)
    elif variant.startswith('vless'):
        uuid = '00000000-0000-4000-8000-000000000001'
        inbound['type'] = outbound['type'] = 'vless'
        inbound['users'] = [{'uuid': uuid}]
        del outbound['password']
        outbound['uuid'] = uuid
        if variant != 'vless':
            protocol = variant.removeprefix('vless-')
            inbound['multiplex'] = {'enabled': True, 'padding': True}
            outbound['multiplex'] = {
                'enabled': True, 'protocol': protocol, 'padding': True,
                'max_connections': 1,
            }
    elif variant == 'timeouts':
        outbound.update(connect_timeout='1s')
        inbound['udp_timeout'] = '1s'
    elif variant.startswith('field-'):
        field = variant.removeprefix('field-')
        outbound[field] = '1s' if 'timeout' in field else 64 * 1024 * 1024
    elif variant == 'brutal':
        inbound.update(up_mbps=100, down_mbps=100)
        outbound.update(up_mbps=100, down_mbps=100)
    return frozen_port


VARIANTS = ('stock', 'generated', 'generated-warp', 'split-same', 'split-port', 'split-user', 'urltest', 'hy2-yamux',
            'hy2-trojan-yamux', 'hy2-trojan-h2mux', 'hy2-trojan-h2mux-one',
            'tuic', 'vless', 'vless-smux', 'vless-yamux', 'vless-h2mux',
            'timeouts', 'brutal', 'field-recv_window_conn', 'field-recv_window',
            'field-idle_timeout')


def generated_configs(router, server, directory, root, kind):
    """Exercise actual deployed generation, including the exit route policy."""
    from router_support import generate

    hy2_port = server['inbounds'][0]['listen_port']
    warp_port = port(socket.SOCK_DGRAM)
    config = json.loads(generate(DOMAIN_AA='127.0.0.1', HY2_DIRECT_PORT=str(hy2_port),
                                 HY2_WARP_PORT=str(warp_port)))
    tag = f'hy2-aa-{kind}'
    router['outbounds'] = [out for out in config['outbounds']
                           if out['tag'] in (tag, f'transport-{tag}', f'pin-{tag}')]
    router['outbounds'].append({'type': 'selector', 'tag': 'foreign-best', 'outbounds': [tag]})
    router['route'] = {'final': f'pin-{tag}'}
    for out in router['outbounds']:
        if out['type'] == 'hysteria2':
            out['tls'] = {'enabled': True, 'server_name': 'localhost',
                          'certificate_path': str(directory / 'cert.pem')}
    values = {'HY2_DIRECT_PORT': str(hy2_port), 'HY2_WARP_PORT': str(warp_port),
              'HY2_MGMT_PORT': str(port(socket.SOCK_DGRAM)), 'HY2_BANDWIDTH_FIELDS': '',
              'WGCF_LOCAL_IPV4': '192.0.2.10', 'WGCF_PEER_ENDPOINT_HOST': '192.0.2.11',
              'WGCF_PEER_ENDPOINT_PORT': '2408', 'WGCF_RESERVED_JSON': '[0,0,0]',
              'WGCF_PRIVKEY': 'wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0=',
              'WGCF_PEER_PUBKEY': 'wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0='}
    for key in ('HY2_PWD_DIRECT', 'HY2_PWD_WARP', 'HY2_PWD_MGMT', 'HY2_OBFS_PWD', 'CLASH_SECRET'):
        values[key] = 'test-password-0123456789'
    rendered = json.loads(Template((root / 'infra/configs/sing-box-server.json.tpl').read_text()).substitute(values))
    for inbound in rendered['inbounds']:
        if inbound.get('tls'):
            inbound['tls']['certificate_path'] = str(directory / 'cert.pem')
            inbound['tls']['key_path'] = str(directory / 'key.pem')
    # Direct regression needs no WARP account or Internet access. Keep the
    # generated rules, but make the unused WARP destination a test direct.
    rendered.pop('endpoints')
    rendered.pop('experimental')
    rendered['outbounds'].append({'type': 'direct', 'tag': 'warp'})
    server.clear()
    server.update(rendered)


def run(router_binary, server_binary, streams=8, variant='stock', origin_address='127.0.0.1',
        server_ssh=None, server_address=None, ssh_key=None, sigstop_after=None, benchmark_rounds=0,
        freeze_wait=4):
    with tempfile.TemporaryDirectory(prefix='hy2-isolation.') as tmp, contextlib.ExitStack() as stack:
        directory = Path(tmp)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                        'ec_paramgen_curve:P-256', '-nodes', '-days', '1', '-subj',
                        '/CN=localhost', '-addext', f'subjectAltName=DNS:localhost,IP:{origin_address}',
                        '-keyout', str(directory / 'key.pem'),
                        '-out', str(directory / 'cert.pem')], check=True, capture_output=True)
        origin = stack.enter_context(Server((origin_address, 0), Origin))
        origin.uploads = queue.Queue()
        worker = threading.Thread(target=origin.serve_forever, daemon=True)
        worker.start()
        stack.callback(worker.join, 2)
        stack.callback(origin.shutdown)
        target_port = origin.server_address[1]
        delay_origin = stack.enter_context(http.server.ThreadingHTTPServer((origin_address, 0), DelayTarget))
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(directory / 'cert.pem', directory / 'key.pem')
        delay_origin.socket = tls.wrap_socket(delay_origin.socket, server_side=True)
        delay_worker = threading.Thread(target=delay_origin.serve_forever, daemon=True)
        delay_worker.start()
        stack.callback(delay_worker.join, 2)
        stack.callback(delay_origin.shutdown)
        udp_origin = stack.enter_context(socketserver.UDPServer((origin_address, 0), UdpTarget))
        udp_worker = threading.Thread(target=udp_origin.serve_forever, daemon=True)
        udp_worker.start()
        stack.callback(udp_worker.join, 2)
        stack.callback(udp_origin.shutdown)
        proxy_port, hy2_port = port(), port(socket.SOCK_DGRAM)
        password = 'isolated-test-password'
        server = {'log': {'level': 'error'}, 'inbounds': [{
            'type': 'hysteria2', 'listen': '127.0.0.1', 'listen_port': hy2_port,
            'users': [{'password': password}],
            'tls': {'enabled': True, 'certificate_path': str(directory / 'cert.pem'),
                    'key_path': str(directory / 'key.pem')},
        }], 'outbounds': [{'type': 'direct'}]}
        router = {'log': {'level': 'error'}, 'inbounds': [{
            'type': 'socks', 'listen': '127.0.0.1', 'listen_port': proxy_port,
        }], 'outbounds': [{
            'type': 'hysteria2', 'tag': 'hy2-test-direct', 'server': '127.0.0.1',
            'server_port': hy2_port, 'password': password,
            'tls': {'enabled': True, 'server_name': 'localhost',
                    'certificate_path': str(directory / 'cert.pem')},
        }]}
        frozen_port = configure_variant(router, server, variant)
        if variant.startswith('generated'):
            from router_support import ROOT
            generated_configs(router, server, directory, ROOT,
                              'warp' if variant == 'generated-warp' else 'direct')
        api_port = port()
        router['experimental'] = {'clash_api': {'external_controller': f'127.0.0.1:{api_port}'}}
        processes = []
        remote_directory = None
        ssh = []
        ssh_options = ['-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                       '-o', 'StrictHostKeyChecking=accept-new',
                       '-o', f'UserKnownHostsFile={directory / "known_hosts"}']
        if ssh_key:
            ssh_options += ['-i', ssh_key]
        if server_ssh:
            ssh = ['ssh', *ssh_options, server_ssh]
            remote_directory = subprocess.check_output(
                [*ssh, 'mktemp -d /tmp/hy2-isolation.XXXXXX'], text=True).strip()
            if not re.fullmatch(r'/tmp/hy2-isolation\.[a-zA-Z0-9]+', remote_directory):
                raise RuntimeError('unexpected remote temporary directory')
            stack.callback(subprocess.run, [*ssh, f'rm -r -- {remote_directory}'],
                           check=True, capture_output=True)
            for out in router['outbounds']:
                if 'server' in out and not out.get('detour'):
                    out['server'] = server_address
            for inbound in server['inbounds']:
                if inbound['type'] in ('hysteria2', 'tuic') or not (
                        variant.startswith('generated') or variant.startswith('hy2-')):
                    inbound['listen'] = '0.0.0.0'

        def stop(process, unit=None):
            if unit:
                subprocess.run([*ssh, f'systemctl stop {unit}'], capture_output=True, timeout=20)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        for name, binary, config in [('server', server_binary, server), ('router', router_binary, router)]:
            config_path = directory / f'{name}.json'
            config_path.write_text(json.dumps(config))
            log = stack.enter_context((directory / f'{name}.log').open('w+'))
            command = [binary, 'run', '-c', str(config_path)]
            unit = None
            if name == 'server' and remote_directory:
                config_path.write_text(json.dumps(config).replace(str(directory), remote_directory))
                subprocess.run(['scp', *ssh_options, str(config_path), str(directory / 'cert.pem'),
                                str(directory / 'key.pem'), f'{server_ssh}:{remote_directory}/'],
                               check=True, capture_output=True)
                unit = Path(remote_directory).name
                command = [*ssh, shlex.join([
                    'systemd-run', '--quiet', '--pipe', '--wait', '--collect', f'--unit={unit}',
                    '--property=RuntimeMaxSec=180s', binary, 'run', '-c', f'{remote_directory}/server.json',
                ])]
            process = subprocess.Popen(command, stdout=log, stderr=log,
                                       env={**os.environ, 'SSL_CERT_FILE': str(directory / 'cert.pem')})
            stack.callback(stop, process, unit)
            processes.append((process, log))
        deadline = time.monotonic() + 10
        while True:
            for process, log in processes:
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read())
            try:
                with socket.create_connection(('127.0.0.1', proxy_port), timeout=.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('router did not start')
                time.sleep(.05)
        # The SOCKS listener can appear before the other process binds UDP.
        deadline = time.monotonic() + 10
        while True:
            baseline = download(proxy_port, target_port, origin_address)
            if baseline['bytes'] == SIZE or time.monotonic() >= deadline:
                break
            time.sleep(.1)
        if baseline['bytes'] != SIZE:
            raise RuntimeError(f'positive control failed: {baseline}')
        benchmarks = []
        for _ in range(benchmark_rounds):
            started = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda _: download(proxy_port, target_port, origin_address, 30), range(8)))
            elapsed = time.monotonic() - started
            if any(result['bytes'] != SIZE for result in results):
                raise RuntimeError(f'benchmark incomplete: {results}')
            benchmarks.append({'bytes': 8 * SIZE, 'seconds': round(elapsed, 3),
                               'mbps': round(8 * SIZE * 8 / elapsed / 1e6, 2)})
        # Closing a dedicated QUIC connection before the stream's FIN/data
        # are acknowledged silently truncates the last TCP upload bytes.
        uploads = []
        upload_size = 1024 * 1024
        for _ in range(10):
            with socks(proxy_port, target_port, target_host=origin_address) as sock:
                read_exact(sock, 5)
                sock.sendall(b'U' + b'z' * upload_size)
                sock.shutdown(socket.SHUT_WR)
                uploads.append(origin.uploads.get(timeout=5))
        if sigstop_after is None:
            for _ in range(streams):
                sock = stack.enter_context(socks(frozen_port, target_port, small=True, target_host=origin_address))
                sock.sendall(b'S')
        else:
            context = multiprocessing.get_context('spawn')
            parent_ready, child_ready = context.Pipe(duplex=False)
            stack.callback(parent_ready.close)
            stack.callback(child_ready.close)
            receiver = context.Process(target=receiving_client,
                                       args=(frozen_port, target_port, origin_address, streams, child_ready))
            receiver.start()
            stack.callback(receiver.join, 5)
            stack.callback(receiver.kill)
            if not parent_ready.poll(10) or not parent_ready.recv():
                raise RuntimeError('receiver did not start')
            time.sleep(sigstop_after)
            os.kill(receiver.pid, signal.SIGSTOP)
        time.sleep(freeze_wait)
        frozen = download(proxy_port, target_port, origin_address)
        selected = router.get('route', {}).get('final', router['outbounds'][0]['tag'])
        selected = selected.removeprefix('pin-')
        if variant.startswith('split-'):
            selected = 'hy2-other-direct'
        query = urllib.parse.urlencode({
            'url': f'https://{origin_address}:{delay_origin.server_address[1]}/', 'timeout': 3000,
        })
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{api_port}/proxies/{selected}/delay?{query}', timeout=4) as response:
                delay = json.load(response)
        except urllib.error.HTTPError as error:
            delay = {'error': error.read().decode()}
        except OSError as error:
            delay = {'error': str(error)}
        udp = udp_probe(proxy_port, origin_address, udp_origin.server_address[1])
        with opener.open(f'http://127.0.0.1:{api_port}/connections', timeout=4) as response:
            chains = sorted({tuple(item['chains']) for item in json.load(response)['connections']})
        print(json.dumps({'variant': variant, 'streams': streams, 'baseline': baseline, 'frozen': frozen,
                          'delay': delay, 'udp': udp, 'chains': chains,
                          'freeze': 'SIGSTOP' if sigstop_after is not None else 'unread',
                          'freeze_wait': freeze_wait,
                          'benchmarks': benchmarks,
                          'upload_expected': upload_size, 'uploads': uploads}), flush=True)
        return (frozen['bytes'] == SIZE and 'delay' in delay and udp['bytes'] > 0
                and all(size == upload_size for size in uploads))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--router-binary', required=True)
    parser.add_argument('--server-binary', required=True)
    parser.add_argument('--streams', type=int, default=8)
    parser.add_argument('--variant', choices=VARIANTS, default='stock')
    parser.add_argument('--origin-address', default='127.0.0.1')
    parser.add_argument('--server-ssh', help='optional isolated exit node, user@host')
    parser.add_argument('--server-address', help='exit address reached by the router')
    parser.add_argument('--ssh-key', help='explicit SSH identity for the isolated exit')
    parser.add_argument('--sigstop-after', type=float, help='read normally, then SIGSTOP the receiver after N seconds')
    parser.add_argument('--benchmark-rounds', type=int, default=0, help='measure eight concurrent downloads before freezing')
    parser.add_argument('--freeze-wait', type=float, default=4, help='seconds before probing frozen readers (default: 4)')
    args = parser.parse_args()
    if args.streams < 1:
        parser.error('--streams must be positive')
    if bool(args.server_ssh) != bool(args.server_address):
        parser.error('--server-ssh and --server-address must be used together')
    if args.freeze_wait < 0 or args.benchmark_rounds < 0 or (args.sigstop_after is not None and args.sigstop_after <= 0):
        parser.error('invalid benchmark count or freeze delay')
    raise SystemExit(0 if run(args.router_binary, args.server_binary, args.streams,
                             args.variant, args.origin_address, args.server_ssh,
                             args.server_address, args.ssh_key, args.sigstop_after,
                             args.benchmark_rounds, args.freeze_wait) else 1)


if __name__ == '__main__':
    main()
