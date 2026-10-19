"""Адрес, появившийся на entry после deploy, закрыт клиентам после перезапуска sing-box.

Запускается от root в своём сетевом пространстве (sudo unshare --net). Стадия 20
повторяется по её же исходнику: вспомогательные скрипты из heredoc, снимок
ENTRY_ADDRS (если стадия его делает), генератор, ExecStartPre юнита sing-box.
Клиент - SOCKS-вход с правилами блока клиентских назначений из конфига.
"""

import contextlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time

from probe_io import read_exact
from router_support import ROOT, SB, run_generator, stop

SOCKS_PORT = 17080
FIRST = ('198.51.100.7', '2001:db8::7')
ADDED = ('198.51.100.8', '2001:db8::8')


class Hello(socketserver.BaseRequestHandler):
    def handle(self):  # noqa - called by TCPServer
        self.request.sendall(b'ok')


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True  # noqa - read by TCPServer.server_bind
    daemon_threads = True  # noqa - read by ThreadingMixIn


class Server6(Server):
    address_family = socket.AF_INET6  # noqa - read by TCPServer.__init__

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


def reach(host, port):
    """Клиент через sing-box получил ответ службы host:port."""
    ip = ipaddress.ip_address(host)
    target = (b'\x01' if ip.version == 4 else b'\x04') + ip.packed + struct.pack('!H', port)
    try:
        with socket.create_connection(('127.0.0.1', SOCKS_PORT), timeout=2) as sock:
            sock.sendall(b'\x05\x01\x00')
            if read_exact(sock, 2, 'SOCKS greeting', 2) != b'\x05\x00':
                return False
            sock.sendall(b'\x05\x01\x00' + target)
            header = read_exact(sock, 4, 'SOCKS reply', 2)
            if header[1] != 0:
                return False
            read_exact(sock, {1: 4, 4: 16}[header[3]] + 2, 'SOCKS reply', 2)
            return read_exact(sock, 2, 'service reply', 2) == b'ok'
    except (OSError, AssertionError, KeyError):
        return False


def tags(rules):
    for rule in rules:
        yield from rule.get('rule_set', [])
        yield from tags(rule.get('rules', []))


with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as cleanup:
    directory = Path(tmp)
    etc = directory / 'etc'
    sbin = directory / 'sbin'
    etc.mkdir()
    sbin.mkdir()
    subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)
    for host in FIRST:
        subprocess.run(['ip', 'address', 'add', host, 'dev', 'lo'], check=True)
    time.sleep(1)  # DAD IPv6 на lo
    for cls, host in ((Server, '0.0.0.0'), (Server6, '::')):
        for port in (443, 8080):
            server = cls((host, port), Hello)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cleanup.callback(server.server_close)
            cleanup.callback(server.shutdown)

    stage = (ROOT / 'infra/scripts/20-ru-router.sh').read_text()

    def local(text):
        text = text.replace('/etc/sing-box/', f'{etc}/')
        return re.sub(r'/usr/local/sbin/([\w.-]+)', lambda m: str(sbin / m.group(1)), text)

    for path, _marker, body in re.findall(
            r"^cat > /usr/local/sbin/(\S+) << '(\w+)'\n(.*?)\n\2$", stage, re.M | re.S):
        (sbin / path).write_text(local(body) + '\n')
        (sbin / path).chmod(0o755)
    unit = stage.split('cat > /etc/systemd/system/sing-box.service', 1)[1].split('\nEOF\n', 1)[0]
    start_pre = [local(line.split('=', 1)[1]) for line in unit.splitlines()
                 if line.startswith('ExecStartPre=')]

    # deploy: снимок адресов в окружение генератора - так, как его делает стадия.
    env = {}
    snapshot = re.search(r'^ENTRY_ADDRS=(\$\(.*\))$', stage, re.M)
    if snapshot:
        env['ENTRY_ADDRS'] = subprocess.run(['bash', '-c', f'echo {snapshot.group(1)}'],
                                            check=True, capture_output=True, text=True).stdout
    generated = run_generator(**env)
    assert generated.returncode == 0, generated.stderr
    cfg = json.loads(generated.stdout)
    rules = [rule for rule in cfg['route']['rules']
             if rule.get('action') == 'reject' and 'inbound' not in rule]
    used = set(tags(rules))
    rule_sets = [json.loads(local(json.dumps(item)))
                 for item in cfg['route']['rule_set'] if item['tag'] in used]
    config = directory / 'config.json'
    config.write_text(json.dumps({
        'log': {'level': 'warn'},
        'inbounds': [{'type': 'socks', 'tag': 'wg-tproxy-in', 'listen': '127.0.0.1',
                      'listen_port': SOCKS_PORT}],
        'outbounds': [{'type': 'direct', 'tag': 'direct'}],
        'route': {'rule_set': rule_sets, 'rules': rules, 'final': 'direct'},
    }))

    def start():
        for command in start_pre:
            result = subprocess.run(command.lstrip('-').split(), capture_output=True, text=True)
            print(result.stdout.strip() or result.stderr.strip())
            if not command.startswith('-'):
                assert result.returncode == 0, result.stderr
        process = subprocess.Popen([SB, 'run', '-c', str(config)], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None, process.stdout.read()
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', SOCKS_PORT)) == 0:
                    return process
            time.sleep(.1)
        stop(process)
        raise AssertionError('sing-box не поднял SOCKS-вход')

    def report(stage_name, hosts):
        failed = False
        for host in hosts:
            closed, public = not reach(host, 8080), reach(host, 443)
            print(f'{stage_name}: {host} служба :8080 {"закрыта" if closed else "ОТКРЫТА"}, '
                  f'публичный :443 {"открыт" if public else "ЗАКРЫТ"}')
            failed |= not (closed and public)
        return failed

    process = start()
    failed = report('после deploy', FIRST)
    for host in ADDED:
        subprocess.run(['ip', 'address', 'add', host, 'dev', 'lo'], check=True)
    time.sleep(1)
    stop(process)
    process = start()
    failed |= report('новый адрес, перезапуск sing-box', ADDED)
    stop(process)
    sys.exit(1 if failed else 0)
