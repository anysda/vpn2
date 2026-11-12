"""Сеть для проверки портов hy2 по сценарию: задержка clash, вход на экзит, ответ UDP.

Случай лежит JSON-файлом в PORTS_CASE, события пишутся строками в PORTS_EVENTS.
Запуск файлом - заглушки команд: `ansible <тег> -m raw -a <команда>` и
`vpn2 ...` - настоящая команда пакета с сетью из сценария.
"""
import json
import os
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'src'


def case():
    return json.loads(Path(os.environ['PORTS_CASE']).read_text())


def event(text):
    with open(os.environ['PORTS_EVENTS'], 'a') as out:
        out.write(text + '\n')


def current_port(tag):
    sys.path.insert(0, str(SRC))
    from vpn2.config import load
    found = next(node for node in load(os.environ['VPN2_CONFIG']).raw['exits'] if node['tag'] == tag)
    return int(found.get('hy2_direct_port', 443))


def delay(tag):
    return case()['delay'].get(f'{tag}:{current_port(tag)}')


def udp_answer(host, port):
    tag = case()['hosts'][host]
    return f'PONG-PING-{port}' if port in case()['open'].get(tag, []) else None


def ansible(tag, command):
    """Код raw-входа на экзит: 4 - не вошли, иначе 0."""
    found = re.search(r'ufw (allow|delete allow) (\d+)/udp', command)
    event(f'ssh {tag} {found[1]} {found[2]}')
    return 4 if tag in case()['ssh_fail'] else 0


def vpn2(argv):
    sys.path.insert(0, str(SRC))
    from vpn2 import cli, config, ports
    write = config.set_exit_port

    def set_exit_port(path, tag, port):
        event(f'set {tag} {port}')
        write(path, tag, port)

    config.set_exit_port, ports.clash_delay, ports.udp_ask = set_exit_port, delay, udp_answer
    ports.time.sleep = lambda _seconds: None
    return cli.main(argv)


if __name__ == '__main__':
    if sys.argv[1] == 'ansible':
        sys.exit(ansible(sys.argv[2], sys.argv[-1]))
    sys.exit(vpn2(sys.argv[sys.argv.index('vpn2.cli') + 1:]))
