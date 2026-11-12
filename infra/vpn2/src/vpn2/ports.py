"""Канал hy2-direct экзитов после 20-ru-router: замер через clash-api entry и подбор порта.

Молчит канал - UDP-пробник по высоким портам: путь есть - пробуется другой порт hy2
(config.yaml, перекатка 10-foreign и 20-ru-router), пути нет вовсе - экзит уходит в
excluded-exits.txt. Стадии перекатывает deploy.sh: команда пишет в fd 3 `rerun <журнал>
<стадия:группа>...` и ждёт код перекатки строкой на stdin; в конце - `summary <строка сводки>`.
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from vpn2 import config

HY2_PORT_CANDIDATES = (443, 4443, 4444, 8443, 8444, 33445)
UDP_PROBE_PORTS = (33445, 39847, 41001, 51234, 60123)
CLASH_URL = ('http://10.99.0.1:9090/proxies/hy2-{}-direct/delay'
             '?timeout=5000&url=http://cp.cloudflare.com/generate_204')
G, Y, R, E = ('\033[32m', '\033[33m', '\033[31m', '\033[0m') if sys.stderr.isatty() else ('',) * 4


def say(text):
    print(text, end='', flush=True)


def fatal(text):
    sys.exit(f'{R}[deploy] FATAL:{E} verify_and_rotate_ports: {text}')


def clash_delay(tag):
    """Задержка канала в мс по clash-api роутера entry или None."""
    try:
        secret = Path('/etc/anysda/clash-secret.txt').read_text().rstrip('\n')
        request = urllib.request.Request(CLASH_URL.format(tag), headers={'Authorization': f'Bearer {secret}'})
        with urllib.request.urlopen(request, timeout=8) as response:
            found = re.search(r'"delay":([1-9][0-9]*)', response.read().decode(errors='replace'))
    except (OSError, ValueError):
        return None
    return int(found[1]) if found else None


def on_exit(tag, command):
    """Команда на экзите через ansible raw; 4 - на экзит не войти по ssh."""
    env = {**os.environ, 'ANSIBLE_CONFIG': f'{os.environ["DEPLOY_ROOT"]}/ansible/ansible.cfg'}
    ansible = Path(os.environ.get('VPN2_ROOT', '/opt/vpn2')) / 'venv/bin/ansible'
    return subprocess.run([ansible, tag, '-m', 'ansible.builtin.raw', '-a', command], env=env,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def udp_ask(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(4)
        try:
            sock.sendto(f'PING-{port}'.encode(), (host, port))
            return sock.recvfrom(1024)[0].decode(errors='replace')
        except OSError:
            return None


def echo(port):
    """Ответ пробнику на экзите: один датаграмм назад с приставкой PONG-."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('', port))
        data, peer = sock.recvfrom(1024)
        sock.sendto(b'PONG-' + data, peer)


def probe_udp(tag, host, port):
    """0 - UDP на порт экзита доходит и возвращается, 1 - нет, 4 - на экзит не войти по ssh."""
    started = on_exit(tag, f'ufw allow {port}/udp >/dev/null 2>&1; nohup timeout 9 /opt/vpn2/venv/bin/vpn2 '
                           f'ports echo {port} </dev/null >/dev/null 2>&1 & sleep 1')
    if started == 4:
        return 4
    answer = udp_ask(host, port)
    on_exit(tag, f'ufw delete allow {port}/udp >/dev/null 2>&1 || true')
    return 0 if answer == f'PONG-PING-{port}' else 1


def rerun(log, *stages):
    """Перекатка стадий руками deploy.sh с выводом в журнал log; False - перекатка упала."""
    path = config.STATE_DIR / log
    os.write(3, f'rerun {path} {" ".join(stages)}\n'.encode())
    if sys.stdin.readline().strip() == '0':
        return True
    tail = path.read_text(errors='replace').splitlines()[-5:] if path.exists() else []
    say(f'{R}    ✗ перекатка упала, полный вывод: {path}{E}\n' + ''.join(f'      {line}\n' for line in tail))
    return False


def exit_port(tag):
    """(порт hy2-direct экзита, порт по умолчанию, адрес) из config.yaml."""
    raw = config.load(os.environ['VPN2_CONFIG']).raw
    default = raw.get('ports', {}).get('hy2_direct', '443')
    item = next(item for item in raw['exits'] if item['tag'] == tag)
    return int(item.get('hy2_direct_port', default)), default, item['host']


def rotate(tag, current):
    """Другой порт hy2 для экзита, к которому UDP доходит; True - канал ожил."""
    path = os.environ['VPN2_CONFIG']
    for port in [port for port in HY2_PORT_CANDIDATES if port != current][:2]:
        say(f'    → пробую hy2 :{port} ')
        config.set_exit_port(path, tag, port)
        if not rerun(f'rotate-{tag}-{port}.log', f'10-foreign:{tag}', '20-ru-router:ru'):
            continue
        time.sleep(4)
        delay = clash_delay(tag)
        if delay:
            say(f'{G}RTT={delay}ms ✓{E}\n')
            return True
        say(f'{R}×{E}\n')
    say(f'  {Y}⚠ hy2 rotation не подобрала рабочий порт для {tag} (UDP-путь есть, но\n'
        f'    hy2-кандидаты режутся). Откатываю порт на дефолт, оставляю как direct-dead.{E}\n')
    config.set_exit_port(path, tag, exit_port(tag)[1])
    rerun(f'rotate-{tag}-rollback.log', f'10-foreign:{tag}', '20-ru-router:ru')
    return False


def check_exit(tag):
    """'ok', 'rotated', 'dead' или 'unknown' (на экзит не вошли)."""
    current, _default, host = exit_port(tag)
    name = f'hy2-{tag}-direct'
    delay = clash_delay(tag)
    if delay:
        say(f'  {name:<16} port={current:<5} {G}RTT={delay}ms ✓{E}\n')
        return 'ok'
    say(f'  {name:<16} port={current:<5} {Y}×{E}\n    UDP-пробник: ')
    for port in UDP_PROBE_PORTS:
        code = probe_udp(tag, host, port)
        if code != 1:
            break
    if code == 4:
        say(f'{R}сбой входа по ssh на экзит {tag} — не проверен, не исключаю, порт не меняю{E}\n')
        return 'unknown'
    if code == 1:
        say(f'{R}все 5 портов фильтруются → exit МЁРТВ, исключаю{E}\n')
        return 'dead'
    say(f'{G}UDP проходит на :{port}{E} — пробую hy2 rotation\n')
    return 'rotated' if rotate(tag, current) else 'unknown'


def check(tags):
    """Проверка всех экзитов; строки сводки деплоя - в fd 3."""
    started = time.time()
    found = {tag: check_exit(tag) for tag in tags}
    dead = [tag for tag in tags if found[tag] == 'dead']
    listed = config.STATE_DIR / 'excluded-exits.txt'
    if dead == tags:
        fatal('все экзиты недостижимы по UDP с RU направления')
    if dead:
        listed.write_text('# Экзиты, исключённые проверкой после 20-ru-router: UDP с entry до них не доходит.\n'
                          '# Чтобы попробовать снова - удалить тег (или весь файл) и перезапустить ./deploy.sh.\n'
                          + ''.join(f'{tag}\n' for tag in dead))
        listed.chmod(0o644)
        say(f'\n  {Y}⚠ Полностью недостижимые экзиты исключены из деплоя: {" ".join(dead)}{E}\n'
            f'    Записано в {listed}\n')
        if not rerun('excluded-reroute.log', '20-ru-router:ru'):
            fatal('entry не пересобран после исключения экзитов')
    else:
        listed.unlink(missing_ok=True)
    seconds = int(time.time() - started)
    took = f'{seconds // 60}m {seconds % 60}s'.removeprefix('0m ')
    say('\n')
    if 'rotated' in found.values():
        os.write(3, f'summary [port-rotate]\tforeign\t{took}\n'.encode())
    if dead:
        os.write(3, f'summary [excluded]\t{" ".join(dead)}\t{took}\n'.encode())


def main(argv):
    """vpn2 ports check ЭКЗИТ... | vpn2 ports echo ПОРТ"""
    if argv[:1] == ['echo'] and len(argv) == 2:
        echo(int(argv[1]))
    elif argv[:1] == ['check'] and len(argv) > 1:
        check(argv[1:])
    else:
        sys.stderr.write(main.__doc__ + '\n')
        return 2
    return 0
