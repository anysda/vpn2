"""Проверки узла его же командами: каждая отдаёт (код, вывод), как команда shell.

Код 0 - проверка прошла; вывод - то, что команда сказала в stdout и stderr.
Цепочка «команда | grep» падает и по коду команды, и по пустому поиску.
"""
import datetime
import json
import math
import os
import re
import subprocess
import time

TARGETS_URL = 'http://127.0.0.1:8428/api/v1/targets'


def code(returncode):
    """Код, каким его видит shell: смерть от сигнала N - 128+N."""
    return returncode if returncode >= 0 else 128 - returncode


def sh(*argv, merge=False, env=None):
    """Команда узла: (код, stdout, stderr); merge кладёт stderr в stdout. Нет команды - 127."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                              text=True, errors='replace', env=env)
    except OSError as error:
        reason = f'{argv[0]}: {error.strerror}\n'
        return (127 if isinstance(error, FileNotFoundError) else 126), reason if merge else '', '' if merge else reason
    return code(done.returncode), done.stdout, done.stderr or ''


def command(*argv):
    """Проверка одной командой: её код и весь вывод."""
    rc, out, _ = sh(*argv, merge=True)
    return rc, out.rstrip('\n')


def chain(*commands):
    """Команды через &&: вывод копится, первая упавшая обрывает цепочку."""
    text = ''
    for argv in commands:
        rc, out, _ = sh(*argv, merge=True)
        text += out
        if rc:
            return rc, text.rstrip('\n')
    return 0, text.rstrip('\n')


def grep(argv, match, *, env=None):
    """`команда | grep`: 1 без подходящей строки, иначе код команды; вывод - её stderr."""
    rc, out, err = sh(*argv, env=env)
    hit = any(match(line) for line in out.splitlines())
    return (rc if hit else 1), err.rstrip('\n')


def service(name):
    return command('systemctl', 'is-active', '--quiet', name)


def forwarding():
    rc, out, err = sh('sysctl', '-n', 'net.ipv4.ip_forward')
    if rc == 0 and out.rstrip('\n') != '1':
        rc = 1
    return rc, err.rstrip('\n')


def firewall():
    rc, text = grep(('ufw', 'status'), lambda line: line == 'Status: active', env={**os.environ, 'LC_ALL': 'C'})
    if rc:
        return rc, text
    rc, out, _ = sh('iptables', '-C', 'INPUT', '-j', 'ufw-before-input', merge=True)
    text += out
    if rc == 0:
        rc, _, err = sh('iptables', '-S', 'ufw-user-input')
        text += err
    return rc, text.rstrip('\n')


def tproxy_chains(iface, chain_name):
    rule = ('-j', 'TPROXY', '--tproxy-mark', '0x42/0x42', '--on-port', '7898', '--on-ip', '127.0.0.1')
    return chain(('iptables', '-t', 'mangle', '-C', 'PREROUTING', '-i', iface, '-j', chain_name),
                 *(('iptables', '-t', 'mangle', '-C', chain_name, '-p', proto, *rule) for proto in ('tcp', 'udp')),
                 ('iptables', '-C', 'INPUT', '-m', 'mark', '--mark', '0x42/0x42', '-j', 'ACCEPT'))


def udp(port):
    return grep(('ss', '-H', '-lun', f'sport = :{port}'), bool)


def tproxy_rule():
    return grep(('ip', 'rule', 'list'), lambda line: re.search(r'fwmark 0x42 lookup 101(\s|$)', line))


def tproxy_route():
    return grep(('ip', 'route', 'show', 'table', '101'), lambda line: re.search(r'^local default dev lo(\s|$)', line))


def recv_queue():
    """Recv-Q сокета tproxy 127.0.0.1:7898 или None: сокета нет или ss упал."""
    rc, out, _ = sh('ss', '-H', '-uln', 'sport = :7898')
    for line in out.splitlines() if rc == 0 else ():
        fields = line.split()
        if len(fields) >= 4 and fields[3].startswith('127.0.0.1:'):
            return fields[1] or None
    return None


def ikev2_conn():
    return grep(('swanctl', '--list-conns'), lambda line: line.startswith('anysda-ikev2:'))


def ikev2_eap_plugin():
    """Плагин в статистике живого charon, а не наличие .so или загрузка плагина самим CLI."""
    rc, out, err = sh('swanctl', '--stats')
    loaded = [name for line in out.splitlines() for found in [re.match(r'\s*loaded plugins: *(.*)', line)] if found
              for name in found.group(1).split(' ') if name == 'eap-mschapv2']
    return (rc if loaded else 1), (err + '\n'.join(loaded)).rstrip('\n')


def targets_problem(raw, since, expected):
    """Что не так со списком targets vmsingle; None - все ждут up и скрейп свежее since."""
    try:
        data = json.loads(raw)
        targets = data['data']['activeTargets']
        if data['status'] != 'success' or not isinstance(targets, list) or not targets:
            raise ValueError('пустой/некорректный список targets')
        seen, failures = set(), []
        for target in targets:
            host = target['labels'].get('host', '?')
            seen.add(host)
            stamp = datetime.datetime.fromisoformat(target['lastScrape'].replace('Z', '+00:00'))
            fresh = stamp.tzinfo is not None and since <= stamp.timestamp() <= time.time()
            health = target['health']
            if health != 'up' or not fresh:
                failures.append(f'{host}: health={health}, свежий={fresh}')
        failures.extend(f'{host}: отсутствует' for host in sorted(expected - seen))
        if failures:
            raise ValueError('; '.join(failures))
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        return f'targets: {error}'
    return None


def mgmt_scrape(tag, expected, timeout, interval):
    """Ждёт, пока все ожидаемые targets станут up по скрейпу свежее начала проверки."""
    since, deadline = time.time(), time.monotonic() + float(timeout)
    lines = [f'[{tag}] mgmt scrape: жду свежие targets up (до {timeout} с)']
    detail = 'API targets недоступен'
    while (left := deadline - time.monotonic()) > 0:
        rc, raw, err = sh('curl', '-fsS', '--max-time', str(max(1, min(5, math.ceil(left)))), TARGETS_URL)
        lines += err.splitlines()
        detail = targets_problem(raw, since, expected) if rc == 0 else 'API targets недоступен'
        if detail is None:
            return 0, '\n'.join(lines + [f'[{tag}] mgmt scrape: все targets up, скрейпы свежее начала проверки'])
        if time.monotonic() < deadline:
            time.sleep(float(interval))
    return 1, '\n'.join(lines + [f'[{tag}] mgmt scrape: таймаут — {detail}'])
