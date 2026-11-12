"""Сеть для проверки портов hy2 по сценарию: задержка clash, вход на экзит, ответ UDP.

Случай лежит JSON-файлом в PORTS_CASE, события пишутся строками в PORTS_EVENTS.
Запуск файлом - заглушки команд: `ansible <тег> -m raw -a <команда>`.
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


if __name__ == '__main__':
    if sys.argv[1] == 'ansible':
        sys.exit(ansible(sys.argv[2], sys.argv[-1]))
    if sys.argv[1] == 'delay':
        value = delay(sys.argv[2])
        sys.exit(print(value) if value else 1)
    if sys.argv[1] == 'udp':
        code = sys.argv[-1]
        host, port = re.search(r"\('([^']+)', (\d+)\)", code).groups()
        answer = udp_answer(host, int(port))
        sys.exit(print(answer) if answer else 124)
