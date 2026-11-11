"""Шаги роли zapret: YouTube с адреса entry через DPI-десинк nfqws2.

Вызываются как `vpn2 zapret <шаг>`; каждый печатает changed=0|1 последней
строкой, шаг facts - JSON для шаблонов роли. Переменные стадии приходят
окружением процесса.

Отбор трафика - только по метке, которую sing-box ставит на сокеты outbound
youtube-ru: списков адресов YouTube здесь нет. Роль идёт до роутера: сначала
готов путь десинка, потом sing-box начинает на него отправлять.
"""

import argparse
import glob
import hashlib
import json
import os
import pwd
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from vpn2 import release, svc

# Пин коммита zapret2 - воспроизводимость сборки. Релиз v1.0.7.
ZAPRET_REF = '038c97e5819df10f8e8c22a2f1fffe57f2f7379b'
ZAPRET_REPO = 'https://github.com/bol-van/zapret2'
NODE_VER = 'v26.13.0'
NODE_SHA256 = 'd28a96af6c38596f6d9d97c0ee531c13d238d757742387d1928de822d959e997'
NODE_URL = f'https://nodejs.org/dist/{NODE_VER}/node-{NODE_VER}-linux-x64.tar.xz'
YT_DLP_VER = '2026.10.26'
# Режет ClientHello. DPI провайдера берёт только split: fake и circular хуже
# или не берут вовсе, не возвращать.
SPLIT = '--payload=tls_client_hello --lua-desync=multisplit:pos=10,midsld:seqovl=1'
LUA_MODULES = ('zapret-lib', 'zapret-antidpi', 'zapret-auto')
DIAG_USER = 'ytzdiag'
PROBE_VENV = '/opt/anysda-yt-probe'
SYSTEM_PYTHON = '/usr/bin/python3'
UNIT_DIR = '/etc/systemd/system'
NFT_CONF = '/etc/nftables-yt-zapret.conf'
QUIC_NFT_CONF = '/etc/nftables-yt-quic.conf'
STAMP = '/var/anysda/.stamps/19-yt-zapret'
MANIFEST = '/tmp/anysda/release-manifest.json'
POT_INSTALL = '/tmp/anysda/install-yt-pot.sh'
BUILD_LOG = '/tmp/anysda-nfqws2-build.log'
NFQUEUE = '/proc/net/netfilter/nfnetlink_queue'
WHEEL_STAMP = '/opt/vpn2/venv/.vpn2-wheel'
PROBE_STATE = '/var/lib/anysda-yt-failover/state.json'
QUIC_FILES = (f'{UNIT_DIR}/anysda-yt-quic.service', f'{UNIT_DIR}/anysda-yt-quic-refresh.service',
              f'{UNIT_DIR}/anysda-yt-quic-refresh.timer', '/usr/local/sbin/anysda-yt-quic-refresh.sh',
              QUIC_NFT_CONF)
FAILOVER_FILES = (f'{UNIT_DIR}/anysda-yt-failover.timer', f'{UNIT_DIR}/anysda-yt-failover.service',
                  '/usr/local/bin/anysda-yt-failover', PROBE_STATE)
DESYNC_FILES = (f'{UNIT_DIR}/anysda-yt-nfqws.service', f'{UNIT_DIR}/anysda-yt-nft.service',
                f'{UNIT_DIR}/anysda-yt-offload.service',
                f'{UNIT_DIR}/nftables.service.d/90-anysda-youtube.conf',
                f'{UNIT_DIR}/sing-box.service.d/90-anysda-youtube.conf',
                '/usr/local/sbin/anysda-yt-nfqws-ready', '/usr/local/bin/anysda-yt-check',
                NFT_CONF, '/etc/sysctl.d/97-anysda-yt-zapret.conf',
                '/etc/modules-load.d/anysda-yt-zapret.conf')


class StageError(Exception):
    pass


def settings(environ):
    """Переменные стадии с умолчаниями; пустое значение равно незаданному."""
    def value(name, default):
        return environ.get(name) or default

    quic = value('YT_QUIC', 'block')
    if quic not in ('block', 'allow'):
        raise StageError(f'неверный YT_QUIC={quic}')
    window = value('YT_ZAPRET_WINDOW_SCALE', '1')
    if not re.fullmatch(r'native|[0-9]|1[0-4]', window):
        raise StageError('YT_ZAPRET_WINDOW_SCALE: допустимо native или 0..14')
    # wssize:wsize=1 - ТСПУ режет и ответ сервера у части узлов googlevideo;
    # помогает только окно клиента в пару байт на первых пакетах. Окно равно
    # wsize << scale, поэтому по умолчанию scale=1. native оставляет масштаб
    # ядра с самого SYN. Явная youtube.strategy имеет приоритет.
    scale = '' if window == 'native' else f':scale={window}'
    base = value('YT_ZAPRET_BASE', '/opt/zapret2')
    return {
        'route': value('YT_ROUTE', 'off'),
        'quic': quic,
        'mark': value('YT_MARK', '256'),
        'queue': value('YT_ZAPRET_QUEUE', '200'),
        'repo': value('YT_ZAPRET_REPO', ZAPRET_REPO),
        'ref': value('YT_ZAPRET_REF', ZAPRET_REF),
        'base': base,
        'bin': f'{base}/binaries/my/nfqws2',
        'strategy': value('YT_ZAPRET_STRATEGY', f'--lua-desync=wssize:wsize=1{scale} {SPLIT}'),
        # Масштаб окна, который ядро ставит в свой SYN. Пусто - замерить.
        'wscale': value('YT_ZAPRET_WSCALE', ''),
        # Выключение TSO/GSO бьёт по скорости всей ноды, поэтому только по просьбе.
        'offload': value('YT_ZAPRET_OFFLOAD', 'keep'),
    }


def wan_iface():
    out = subprocess.run(['ip', '-4', '-o', 'route', 'show', 'default'],
                         capture_output=True, text=True, check=True).stdout
    fields = out.splitlines()[0].split() if out.strip() else []
    if len(fields) < 5:
        raise StageError('не удалось определить WAN-интерфейс')
    return fields[4]


def kernel_wscale():
    """Соединение по loopback строится той же функцией ядра, TCP_INFO отдаёт его rcv_wscale."""
    try:
        with socket.create_server(('127.0.0.1', 0)) as server, \
                socket.create_connection(server.getsockname()) as client:
            # struct tcp_info: 7-й байт - snd_wscale:4, rcv_wscale:4.
            return str(client.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 8)[6] >> 4)
    except OSError:
        return ''


def window_rules(cfg, wan, measure=kernel_wscale):
    """Правила nft, возвращающие окну сдвиг, который съел scale из стратегии.

    wssize подменяет масштаб окна в SYN, и сервер до конца соединения читает
    окно клиента как window << scale, а ядро пишет окно под свой масштаб.
    Сервер видит окно в 2^(wscale-scale) раз меньше настоящего. Недостающий
    сдвиг возвращается на каждом исходящем не-SYN пакете.
    """
    found = re.search(r'\s--lua-desync=wssize(:\S*)?\s', f" {cfg['strategy']} ")
    scale = re.search(r':scale=([0-9]+):', (found[1] or '') + ':') if found else None
    if not scale:
        return ''
    requested = int(scale[1])
    kernel = cfg['wscale'] or measure()
    if not re.fullmatch(r'[0-9]|1[0-4]', kernel):
        # 9 - замер на проде: rmem_max=16 МБ из стадии 00.
        print(f"масштаб окна ядра не определился ('{kernel}') - беру 9")
        kernel = '9'
    # zapret2 только уменьшает scale: запрошенный выше ядерного SYN не меняет.
    shift = int(kernel) - min(requested, int(kernel))
    if shift <= 0:
        return ''
    match = f'meta mark {cfg["mark"]} oifname "{wan}" tcp dport 443 tcp flags & syn == 0'
    limit = 65535 >> shift
    # Окно, которое после сдвига не влезает в 16 бит, - потолок 65535.
    return (f'        {match} tcp window > {limit} counter tcp window set 65535\n'
            f'        {match} tcp window <= {limit} counter tcp window set tcp window << {shift}')


def _quiet(*argv):
    return subprocess.run(argv, capture_output=True, check=False).returncode == 0


def _remove(paths):
    removed = False
    for path in paths:
        if os.path.lexists(path):
            os.unlink(path)
            removed = True
    return removed


def quic_off():
    """Сначала остановить таймер и уже запущенное им обновление: иначе оно вернёт таблицу."""
    _quiet('systemctl', 'disable', '--now', 'anysda-yt-quic-refresh.timer')
    _quiet('systemctl', 'stop', 'anysda-yt-quic-refresh.service')
    _quiet('systemctl', 'disable', '--now', 'anysda-yt-quic.service')
    table = _quiet('nft', 'delete', 'table', 'inet', 'anysda_ytquic')
    removed = _remove(QUIC_FILES)
    if removed:
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
    return table or removed


def teardown():
    """Выключение - такая же штатная операция, как включение.

    Исходники и бинарник остаются: пересборка занимает минуты, обратное
    включение должно быть мгновенным.
    """
    _quiet('systemctl', 'disable', '--now', 'anysda-yt-failover.timer')
    _quiet('systemctl', 'stop', 'anysda-yt-failover.service')
    changed = _remove(FAILOVER_FILES)
    changed |= quic_off()
    for unit in ('anysda-yt-nfqws', 'anysda-yt-nft', 'anysda-yt-offload'):
        _quiet('systemctl', 'disable', '--now', f'{unit}.service')
    changed |= _quiet('nft', 'delete', 'table', 'inet', 'ytzapret')
    changed |= _remove(DESYNC_FILES)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    changed |= _remove([STAMP])
    return changed


def fetch_source(environ, cfg):
    """Исходники zapret2 на закреплённом коммите: архив выпуска, в разработке - git."""
    base, ref = Path(cfg['base']), cfg['ref']
    if environ.get('VPN2_DEVELOPMENT', 'false') != 'true':
        with tempfile.TemporaryDirectory() as work:
            archive = os.path.join(work, 'source.tar.gz')
            if release.main(['--manifest', environ.get('VPN2_RELEASE_MANIFEST') or MANIFEST, 'artifact',
                             'zapret2-source', archive, '--url', f"{cfg['repo']}/archive/{ref}.tar.gz"]):
                raise StageError('архив zapret2 не получен')
            marker = base / '.release-ref'
            if marker.is_file() and marker.read_text().strip() == ref:
                return False
            source = os.path.join(work, 'source')
            os.mkdir(source)
            subprocess.run(['tar', '-xzf', archive, '--strip-components=1', '-C', source], check=True)
            base.mkdir(parents=True, exist_ok=True)
            subprocess.run(['cp', '-a', f'{source}/.', f'{base}/'], check=True)
            marker.write_text(ref + '\n')
        return True
    git = ['git', '-C', str(base)]
    if not (base / '.git').is_dir():
        subprocess.run(['rm', '-rf', str(base)], check=True)
        subprocess.run(['git', 'clone', '-q', cfg['repo'], str(base)], check=True)
    elif subprocess.run([*git, 'rev-parse', 'HEAD'], capture_output=True, text=True,
                        check=True).stdout.strip() == ref:
        return False
    else:
        subprocess.run([*git, 'fetch', '-q', '--all'], check=True)
    subprocess.run([*git, 'checkout', '-q', ref], check=True)
    return True


def build(cfg, changed):
    """Никаких `make -q`/`make -n` по этому Makefile.

    Цель all зависит от clean, чей рецепт - одна строка с $(MAKE), а такие
    строки GNU make выполняет даже в «вопросительных» режимах: сухой прогон
    стирает binaries/my, и демон живёт на удалённом inode до первого рестарта.
    """
    binary = cfg['bin']
    if changed or not os.access(binary, os.X_OK):
        with open(BUILD_LOG, 'wb') as log:
            failed = subprocess.run(['make', '-C', cfg['base']], stdout=log, stderr=subprocess.STDOUT,
                                    check=False).returncode
        if failed:
            tail = Path(BUILD_LOG).read_text(errors='replace').splitlines()[-25:]
            raise StageError('сборка nfqws2 упала, хвост лога:\n' + '\n'.join(tail))
        changed = True
    if not os.access(binary, os.X_OK):
        raise StageError(f'{binary} не собрался')
    version = subprocess.run([binary, '--version'], capture_output=True, text=True, check=False)
    print((version.stdout + version.stderr).splitlines()[0])
    return changed


def lua_init(cfg):
    args = ''
    for module in LUA_MODULES:
        path = f"{cfg['base']}/lua/{module}.lua"
        if not os.path.isfile(path):
            raise StageError(f"нет lua-модуля {path} - nfqws2 упадёт с 'desync function does not exist'")
        args += f' --lua-init=@{path}'
    return args


def shell_values(cfg):
    """Значения для скрипта проверки - словами оболочки: настройка остаётся данными."""
    return {f'{name}_sh': shlex.quote(str(cfg[name])) for name in ('mark', 'base', 'queue')}


def facts(environ):
    """Значения для шаблонов роли; диагностический пользователь к этому шагу уже заведён."""
    cfg = settings(environ)
    wan = wan_iface()
    diag = pwd.getpwnam(DIAG_USER)
    rules = window_rules(cfg, wan)
    return {
        'mark': cfg['mark'], 'queue': cfg['queue'], 'base': cfg['base'], 'bin': cfg['bin'],
        'strategy': cfg['strategy'], 'lua_init': lua_init(cfg), 'wan': wan, 'offload': cfg['offload'],
        'diag_uid': diag.pw_uid, 'diag_gid': diag.pw_gid, **shell_values(cfg),
        'window_rules': rules or '        # (стратегия не меняет масштаб окна — поправка не нужна)',
    }


def offload_keep():
    """Офлоад оставлен как есть: юнит прежнего выбора off убирается."""
    unit = f'{UNIT_DIR}/anysda-yt-offload.service'
    if not os.path.isfile(unit):
        return False
    _quiet('systemctl', 'disable', '--now', 'anysda-yt-offload.service')
    os.unlink(unit)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    return True


def _main_pid():
    out = subprocess.run(['systemctl', 'show', '-p', 'MainPID', '--value', 'anysda-yt-nfqws.service'],
                         capture_output=True, text=True, check=False).stdout.strip()
    return out if out.isdigit() and out != '0' else ''


def queue_bound(queue, pid, table=NFQUEUE):
    """Демон готов, когда сам держит свою очередь: portid в nfnetlink_queue равен PID."""
    try:
        rows = Path(table).read_text().splitlines()
    except OSError:
        return False
    return bool(pid) and any(row.split()[:2] == [str(queue), str(pid)] for row in rows)


def restart(environ):
    """Перезапуск цепочек и демона только при смене их входов.

    Пока nfqws2 перезапускается, `queue ... bypass` пропускает YouTube мимо
    десинка. Рестарт nft-цепочек systemd через Requires= тянет и nfqws2.
    """
    cfg = settings(environ)
    changed = svc.restart_if_changed('anysda-yt-nft.service', [f'{UNIT_DIR}/anysda-yt-nft.service', NFT_CONF])
    restarted = svc.restart_if_changed('anysda-yt-nfqws.service', [
        f'{UNIT_DIR}/anysda-yt-nfqws.service', '/usr/local/sbin/anysda-yt-nfqws-ready', cfg['bin'],
        *sorted(glob.glob(f"{cfg['base']}/lua/*.lua"))])
    if changed or restarted:
        for _ in range(10):
            if queue_bound(cfg['queue'], _main_pid()):
                break
            time.sleep(0.2)
    # Демон обязан исполнять тот файл, что лежит на диске: после пересборки
    # живой процесс может держать удалённый inode и не пережить рестарта.
    pid = _main_pid()
    if pid:
        try:
            stale = os.stat(f'/proc/{pid}/exe').st_ino != os.stat(cfg['bin']).st_ino
        except OSError:
            stale = True
        if stale:
            print('демон на устаревшем inode - рестарт')
            subprocess.run(['systemctl', 'restart', 'anysda-yt-nfqws.service'], check=True)
            time.sleep(2)
            restarted = True
    return changed or restarted


def _output(*argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        return ''


def _pot_state(venv):
    marker = Path(venv, 'bgutil/.installed')
    version = _output(f'{venv}/bin/python3', '-c',
                      'from importlib.metadata import version; print(version("bgutil-ytdlp-pot-provider"))')
    return version, marker.stat().st_ino if marker.is_file() else 0


def install_probe(environ, venv=PROBE_VENV):
    """Видеопроба под диагностическим пользователем: yt-dlp с EJS, свой Node и PO-токены.

    EJS требует Node >=22, системный пакет может быть старее; Node других
    приложений узла не трогаем. Роутер поддерживает linux-amd64.
    """
    changed = False
    if _output(f'{venv}/bin/yt-dlp', '--version') != YT_DLP_VER:
        subprocess.run([SYSTEM_PYTHON, '-m', 'venv', venv], check=True)
        subprocess.run([f'{venv}/bin/pip', 'install', '--disable-pip-version-check',
                        f'yt-dlp[default]=={YT_DLP_VER}'], check=True, stdout=sys.stderr)
        changed = True
    npm = f'{venv}/lib/node_modules/npm'
    if _output(f'{venv}/bin/node', '--version') != NODE_VER or not os.path.isfile(f'{npm}/bin/npm-cli.js'):
        with tempfile.TemporaryDirectory() as work:
            archive, tree = os.path.join(work, 'node.tar.xz'), f'node-{NODE_VER}-linux-x64'
            argv = ['artifact', 'yt-node', archive, '--url', NODE_URL, '--sha256', NODE_SHA256]
            if environ.get('VPN2_DEVELOPMENT', 'false') != 'true':
                argv = ['--manifest', environ.get('VPN2_RELEASE_MANIFEST') or MANIFEST, *argv]
            if release.main(argv):
                raise StageError('архив Node не получен')
            if hashlib.sha256(Path(archive).read_bytes()).hexdigest() != NODE_SHA256:
                raise StageError('архив Node не сошёлся с SHA-256')
            subprocess.run(['tar', '-xJf', archive, '-C', work, f'{tree}/bin/node',
                            f'{tree}/lib/node_modules/npm'], check=True)
            subprocess.run(['install', '-m', '0755', f'{work}/{tree}/bin/node', f'{venv}/bin/node'], check=True)
            os.makedirs(f'{venv}/lib/node_modules', exist_ok=True)
            subprocess.run(['cp', '-a', f'{work}/{tree}/lib/node_modules/npm', f'{venv}/lib/node_modules/'],
                           check=True)
        changed = True
    before = _pot_state(venv)
    subprocess.run(['bash', environ.get('VPN2_YT_POT_INSTALL') or POT_INSTALL, venv], check=True,
                   stdout=sys.stderr, env={**environ, 'PATH': f"{os.path.dirname(sys.executable)}:{environ['PATH']}"})
    return changed or _pot_state(venv) != before


def remeasure():
    """Результат в state.json старше кода пробы: перемерить сразу, таймер придёт через 5 минут."""
    try:
        stale = os.stat(WHEEL_STAMP).st_mtime > os.stat(PROBE_STATE).st_mtime
    except OSError:
        stale = os.path.exists(WHEEL_STAMP)
    if stale:
        _quiet('systemctl', 'start', 'anysda-yt-failover.service')
    return False


STEPS = ('off', 'quic-off', 'source', 'facts', 'offload-keep', 'restart', 'probe', 'remeasure')


def main(argv):
    parser = argparse.ArgumentParser(prog='vpn2 zapret')
    parser.add_argument('step', choices=STEPS)
    args = parser.parse_args(argv)
    environ = os.environ
    try:
        if args.step == 'facts':
            print(json.dumps(facts(environ), ensure_ascii=False, sort_keys=True))
            return 0
        cfg = settings(environ)
        changed = {
            'off': teardown,
            'quic-off': quic_off,
            'source': lambda: build(cfg, fetch_source(environ, cfg)),
            'offload-keep': offload_keep,
            'restart': lambda: restart(environ),
            'probe': lambda: install_probe(environ),
            'remeasure': remeasure,
        }[args.step]()
    except (StageError, KeyError, OSError, subprocess.CalledProcessError) as error:
        sys.stderr.write(f'vpn2 zapret {args.step}: {error}\n')
        return 1
    print(f'changed={int(changed)}')
    return 0
