"""Готовность entry: службы VPN активны, панель отвечает, управляющий трафик проходит."""

import socket
import subprocess
from urllib import request

ENTRY_UNITS = ('sing-box', 'wg-quick@wg0', 'openvpn-server@server', 'strongswan-starter',
               'anysda-ikev2-routing')
# Панель отвечает, только когда база восстановлена и конфиги применены.
PANEL_URL = 'http://127.0.0.1:51821/api/version'
# Управляющий трафик: собственный прокси entry выходит через экзиты.
SOCKS = ('127.0.0.1', 7897)
CONTROL_TARGET = ('cp.cloudflare.com', 80)
ENTRY_PORT = 443
# Один на процесс: opener на каждый круг растит процесс навсегда.
OPENER = request.build_opener(request.ProxyHandler({}))


def socks_connect(proxy, target, timeout=5):
    host, port = target
    name = host.encode()
    with socket.create_connection(proxy, timeout=timeout) as sock:
        sock.sendall(b'\5\1\0')
        if sock.recv(2) != b'\5\0':
            return False
        sock.sendall(b'\5\1\0\3' + bytes([len(name)]) + name + port.to_bytes(2, 'big'))
        reply = sock.recv(10)
        return len(reply) >= 2 and reply[1] == 0


def entry_ready(settings):
    units = settings.get('units', ENTRY_UNITS)
    try:
        states = subprocess.run(['systemctl', 'is-active', *units], capture_output=True,
                                text=True, timeout=10).stdout.split()
        if states != ['active'] * len(units):
            return False
        with OPENER.open(settings.get('panel', PANEL_URL), timeout=5) as response:
            if response.status != 200:
                return False
        return socks_connect(tuple(settings.get('socks', SOCKS)),
                             tuple(settings.get('target', CONTROL_TARGET)))
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def reachable(host, port, timeout=5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
