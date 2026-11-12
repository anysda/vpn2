"""Готовность entry: службы VPN активны, панель отвечает, управляющий трафик проходит."""

import socket
import subprocess

ENTRY_UNITS = ('sing-box', 'wg-quick@wg0', 'openvpn-server@server', 'strongswan-starter',
               'anysda-ikev2-routing')
# Панель отвечает, только когда база восстановлена и конфиги применены.
PANEL_URL = 'http://127.0.0.1:51821/api/version'
# Управляющий трафик: собственный прокси entry выходит через экзиты.
SOCKS = '127.0.0.1:7897'
CONTROL_URL = 'http://cp.cloudflare.com/generate_204'
ENTRY_PORT = 443


def _run(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def entry_ready():
    states = _run('systemctl', 'is-active', *ENTRY_UNITS)
    if not states or states.stdout.split() != ['active'] * len(ENTRY_UNITS):
        return False
    panel = _run('curl', '-sf', '-o', '/dev/null', '--noproxy', '*', '--max-time', '5', PANEL_URL)
    # Любой HTTP-ответ через прокси - трафик дошёл.
    control = panel and panel.returncode == 0 and _run(
        'curl', '-s', '-o', '/dev/null', '--max-time', '5', '--socks5-hostname', SOCKS, CONTROL_URL)
    return bool(control) and control.returncode == 0


def reachable(host, port=ENTRY_PORT, timeout=5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
