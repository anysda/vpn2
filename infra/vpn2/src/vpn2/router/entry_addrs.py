"""Глобальные адреса интерфейсов entry -> набор sing-box entry-addrs.

`vpn2 router entry-addrs` идёт перед каждым стартом sing-box (ExecStartPre):
клиентам VPN закрыты службы на адресах самого entry.
"""
import ipaddress
import json
import os
import subprocess
import sys
import tempfile

SET = '/etc/sing-box/entry-addrs.json'


def addresses():
    out = subprocess.run(['ip', '-j', 'addr', 'show', 'scope', 'global'],
                         check=True, capture_output=True, text=True, timeout=10).stdout
    nets = {ipaddress.ip_network(info['local'])
            for link in json.loads(out) for info in link.get('addr_info', [])
            if info.get('scope') == 'global' and info.get('local')}
    return [str(net) for net in sorted(nets, key=lambda n: (n.version, n))]


def write(nets):
    body = json.dumps({'version': 2, 'rules': [{'ip_cidr': nets}] if nets else []}, indent=2)
    try:
        with open(SET) as f:
            if f.read() == body:
                return
    except FileNotFoundError:
        pass
    with tempfile.NamedTemporaryFile('w', dir=os.path.dirname(SET), delete=False,
                                     prefix='.entry-addrs.') as tmp:
        tmp.write(body)
    os.chmod(tmp.name, 0o600)
    os.replace(tmp.name, SET)


def main(argv=None):
    del argv
    try:
        nets = addresses()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        # Без файла набора sing-box не стартует: тогда пустой набор (частные сети
        # закрыты и так), иначе остаются адреса прошлого старта.
        kept = os.path.exists(SET)
        if not kept:
            write([])
        print(f'vpn2-entry-addrs: адреса entry не прочитаны: {e}; '
              f'{"набор прошлого старта" if kept else "набор пуст"}', file=sys.stderr)
        sys.exit(1)
    write(nets)
    print(f'vpn2-entry-addrs: клиентам закрыты службы на {" ".join(nets) or "-"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
