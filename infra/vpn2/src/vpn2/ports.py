"""Канал hy2-direct экзитов после 20-ru-router: замер через clash-api entry и подбор порта.

Молчит канал - UDP-пробник по высоким портам: путь есть - пробуется другой порт hy2
(config.yaml, перекатка 10-foreign и 20-ru-router), пути нет вовсе - экзит уходит в
excluded-exits.txt. Стадии перекатывает деплой: check получает от него run(журнал, [(стадия, группа)...]).
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from vpn2 import config
from vpn2.probe import ClashApi

UDP_PROBE_PORTS = (33445, 39847, 41001, 51234, 60123)
CLASH_SECRET = Path('/etc/anysda/clash-secret.txt')
# Окно готовности sing-box после перезапуска: 6 проб через 5 с, до ~70 с на молчащий канал.
READY_ATTEMPTS, READY_PAUSE = 6, 5
G, Y, R, E = ('\033[32m', '\033[33m', '\033[31m', '\033[0m') if sys.stderr.isatty() else ('',) * 4


def say(text):
    print(text, end='', flush=True)


def fatal(text):
    sys.exit(f'{R}[deploy] FATAL:{E} verify_and_rotate_ports: {text}')


def probe_delay(api, tag):
    """Одна проба hy2 через clash-api роутера entry: мс или None.

    Сам hysteria2, мимо селектора: на запасном TCP канал жив и при режущемся порте hy2.
    """
    delay, _reason = api.delay(f'transport-hy2-{tag}-direct', 'http://cp.cloudflare.com/generate_204',
                               timeout_ms=5000, http_timeout=8)
    return delay


def clash_delay(tag):
    """Задержка канала в мс или None, если канал молчит все READY_ATTEMPTS проб.

    Сразу после 20-ru-router sing-box ещё поднимается: clash-api не отвечает или первая
    проба hy2 падает. Один промах ещё не значит, что порт режут, - иначе перекатка
    здорового экзита.
    """
    try:
        secret = CLASH_SECRET.read_text().rstrip('\n')
    except OSError:
        return None
    api = ClashApi('http://10.99.0.1:9090', secret)
    for attempt in range(READY_ATTEMPTS):
        if attempt:
            time.sleep(READY_PAUSE)
        delay = probe_delay(api, tag)
        if delay:
            return delay
    return None


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


def rerun(run, log, *stages):
    """Перекатка стадий деплоем с выводом в журнал log; False - перекатка упала."""
    path = config.STATE_DIR / log
    if run(path, [tuple(stage.split(':')) for stage in stages]):
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


def rotate(tag, current, run):
    """Другой порт hy2 для экзита, к которому UDP доходит; True - канал ожил."""
    path = os.environ['VPN2_CONFIG']
    for port in [port for port in config.HY2_PORT_CANDIDATES if port != current][:2]:
        say(f'    → пробую hy2 :{port} ')
        config.set_exit_port(path, tag, port)
        if not rerun(run, f'rotate-{tag}-{port}.log', f'10-foreign:{tag}', '20-ru-router:ru'):
            continue
        delay = clash_delay(tag)
        if delay:
            say(f'{G}RTT={delay}ms ✓{E}\n')
            return True
        say(f'{R}×{E}\n')
    say(f'  {Y}⚠ hy2 rotation не подобрала рабочий порт для {tag} (UDP-путь есть, но\n'
        f'    hy2-кандидаты режутся). Откатываю порт на дефолт, оставляю как direct-dead.{E}\n')
    config.set_exit_port(path, tag, exit_port(tag)[1])
    rerun(run, f'rotate-{tag}-rollback.log', f'10-foreign:{tag}', '20-ru-router:ru')
    return False


def check_exit(tag, run):
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
    return 'rotated' if rotate(tag, current, run) else 'unknown'


def check(tags, run):
    """Проверка всех экзитов; итог - строки сводки деплоя «стадия\tгруппа\tвремя»."""
    started = time.time()
    found = {tag: check_exit(tag, run) for tag in tags}
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
        if not rerun(run, 'excluded-reroute.log', '20-ru-router:ru'):
            fatal('entry не пересобран после исключения экзитов')
    else:
        listed.unlink(missing_ok=True)
    seconds = int(time.time() - started)
    took = f'{seconds // 60}m {seconds % 60}s'.removeprefix('0m ')
    say('\n')
    return ([f'[port-rotate]\tforeign\t{took}'] if 'rotated' in found.values() else []) + \
        ([f'[excluded]\t{" ".join(dead)}\t{took}'] if dead else [])


def main(argv):
    """vpn2 ports echo ПОРТ"""
    if argv[:1] == ['echo'] and len(argv) == 2:
        echo(int(argv[1]))
    else:
        sys.stderr.write(main.__doc__ + '\n')
        return 2
    return 0
