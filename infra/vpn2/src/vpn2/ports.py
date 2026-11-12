"""Канал hy2-direct экзитов после 20-ru-router: замер через clash-api entry и подбор порта.

Молчит канал - UDP-пробник по высоким портам: путь есть - пробуется другой порт hy2
(config.yaml, перекатка 10-foreign и 20-ru-router), пути нет вовсе - экзит уходит в
excluded-exits.txt. Стадии перекатывает deploy.sh: команда кладёт в <state>/rerun журнал
и строки стадия:группа и выходит, следующий вызов получает код перекатки (--rerun-rc) и
доигрывает с того же места. Прошлые замеры читаются из <state>/facts.json, их вывод не повторяется.
"""
import json
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
EXCLUDED_HEAD = ('# Экзиты, исключённые проверкой после 20-ru-router: UDP с entry до них не доходит.\n'
                 '# Чтобы попробовать снова - удалить тег (или весь файл) и перезапустить ./deploy.sh.\n')
G, Y, R, E = ('\033[32m', '\033[33m', '\033[31m', '\033[0m') if sys.stderr.isatty() else ('',) * 4


class Rerun(Exception):
    """Нужна перекатка стадий: план записан, проверка продолжится следующим вызовом."""


class Fatal(Exception):
    pass


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


class Run:
    """Замеры и коды перекаток по порядку; пока повторяется прошлое, вывод молчит."""

    def __init__(self, folder, rerun_rc):
        self.folder, self.facts_file = folder, folder / 'facts.json'
        self.facts = json.loads(self.facts_file.read_text()) if self.facts_file.exists() else [time.time()]
        if rerun_rc is not None:
            self.facts.append(rerun_rc)
            self.save()
        self.seen = 1

    def save(self):
        self.facts_file.write_text(json.dumps(self.facts))

    @property
    def live(self):
        return self.seen >= len(self.facts)

    def fact(self, measure):
        if self.live:
            self.facts.append(measure())
            self.save()
        self.seen += 1
        return self.facts[self.seen - 1]

    def say(self, text):
        if self.live:
            print(text, end='', flush=True)

    def rerun(self, log, *pairs):
        """Перекатка стадий с выводом в журнал log; False - перекатка упала."""
        path = config.STATE_DIR / log

        def request():
            (self.folder / 'rerun').write_text('\n'.join((str(path), *pairs)) + '\n')
            raise Rerun

        code = self.fact(request)
        if code and self.live:
            tail = path.read_text(errors='replace').splitlines()[-5:] if path.exists() else []
            self.say(f'{R}    ✗ перекатка упала, полный вывод: {path}{E}\n' + ''.join(f'      {line}\n' for line in tail))
        return code == 0


def exit_port(tag):
    """(порт hy2-direct экзита, порт по умолчанию, адрес) из config.yaml, исключён экзит или нет."""
    raw = config.load(os.environ['VPN2_CONFIG']).raw
    default = raw.get('ports', {}).get('hy2_direct', '443')
    item = next(item for item in raw['exits'] if item['tag'] == tag)
    return int(item.get('hy2_direct_port', default)), default, item['host']


def rotate(run, tag, current):
    """Другой порт hy2 для экзита, к которому UDP доходит; True - канал ожил."""
    path = os.environ['VPN2_CONFIG']
    for port in [port for port in HY2_PORT_CANDIDATES if port != current][:2]:
        run.say(f'    → пробую hy2 :{port} ')
        if run.live:
            config.set_exit_port(path, tag, port)
        if not run.rerun(f'rotate-{tag}-{port}.log', f'10-foreign:{tag}', '20-ru-router:ru'):
            continue
        if run.live:
            time.sleep(4)
        delay = run.fact(lambda: clash_delay(tag))
        if delay:
            run.say(f'{G}RTT={delay}ms ✓{E}\n')
            return True
        run.say(f'{R}×{E}\n')
    run.say(f'  {Y}⚠ hy2 rotation не подобрала рабочий порт для {tag} (UDP-путь есть, но\n'
            f'    hy2-кандидаты режутся). Откатываю порт на дефолт, оставляю как direct-dead.{E}\n')
    if run.live:
        config.set_exit_port(path, tag, exit_port(tag)[1])
    run.rerun(f'rotate-{tag}-rollback.log', f'10-foreign:{tag}', '20-ru-router:ru')
    return False


def check_exit(run, tag):
    """'ok', 'rotated', 'dead' или 'unknown' (на экзит не вошли)."""
    # Порт берётся из журнала: повторный вызов видит config.yaml уже с новым портом.
    current, _default, host = run.fact(lambda: exit_port(tag))
    delay = run.fact(lambda: clash_delay(tag))
    if delay:
        run.say(f'  {f"hy2-{tag}-direct":<16} port={current:<5} {G}RTT={delay}ms ✓{E}\n')
        return 'ok'
    run.say(f'  {f"hy2-{tag}-direct":<16} port={current:<5} {Y}×{E}\n    UDP-пробник: ')
    code, alive = 1, None
    for port in UDP_PROBE_PORTS:
        code = run.fact(lambda: probe_udp(tag, host, port))
        alive = port if code == 0 else alive
        if code != 1:
            break
    if code == 4:
        run.say(f'{R}сбой входа по ssh на экзит {tag} — не проверен, не исключаю, порт не меняю{E}\n')
        return 'unknown'
    if alive is None:
        run.say(f'{R}все 5 портов фильтруются → exit МЁРТВ, исключаю{E}\n')
        return 'dead'
    run.say(f'{G}UDP проходит на :{alive}{E} — пробую hy2 rotation\n')
    return 'rotated' if rotate(run, tag, current) else 'unknown'


def check(run, tags):
    """Проверка всех экзитов; строки сводки деплоя на выходе."""
    found = {tag: check_exit(run, tag) for tag in tags}
    dead = [tag for tag in tags if found[tag] == 'dead']
    listed = config.STATE_DIR / 'excluded-exits.txt'
    if dead:
        if dead == list(tags):
            raise Fatal('verify_and_rotate_ports: все экзиты недостижимы по UDP с RU направления')
        listed.write_text(EXCLUDED_HEAD + ''.join(f'{tag}\n' for tag in dead))
        listed.chmod(0o644)
        run.say(f'\n  {Y}⚠ Полностью недостижимые экзиты исключены из деплоя: {" ".join(dead)}{E}\n'
                f'    Записано в {listed}\n')
        if not run.rerun('excluded-reroute.log', '20-ru-router:ru'):
            raise Fatal('verify_and_rotate_ports: entry не пересобран после исключения экзитов')
    else:
        listed.unlink(missing_ok=True)
    seconds = int(time.time() - run.facts[0])
    took = f'{seconds // 60}m {seconds % 60}s'.removeprefix('0m ')
    run.say('\n')
    return ([f'[port-rotate]\tforeign\t{took}'] if 'rotated' in found.values() else []) + \
        ([f'[excluded]\t{" ".join(dead)}\t{took}'] if dead else [])


def main(argv):
    """vpn2 ports check --state КАТАЛОГ [--rerun-rc КОД] ЭКЗИТ... | vpn2 ports echo ПОРТ"""
    if argv[:1] == ['echo'] and len(argv) == 2:
        echo(int(argv[1]))
        return 0
    import argparse
    parser = argparse.ArgumentParser(prog='vpn2 ports check')
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--rerun-rc', type=int)
    parser.add_argument('tags', nargs='+')
    if argv[:1] != ['check']:
        sys.stderr.write(main.__doc__ + '\n')
        return 2
    args = parser.parse_args(argv[1:])
    args.state.mkdir(parents=True, exist_ok=True)
    (args.state / 'rerun').unlink(missing_ok=True)
    run = Run(args.state, args.rerun_rc)
    try:
        lines = check(run, args.tags)
    except Rerun:
        return 0
    except Fatal as error:
        print(f'{R}[deploy] FATAL:{E} {error}', file=sys.stderr, flush=True)
        return 1
    finally:
        if not (args.state / 'rerun').exists():
            run.facts_file.unlink(missing_ok=True)
    (args.state / 'stagelog').write_text(''.join(f'{line}\n' for line in lines))
    return 0
