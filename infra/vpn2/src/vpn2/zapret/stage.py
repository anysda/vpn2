"""Шаги роли zapret: YouTube с адреса entry через DPI-десинк nfqws2.

`vpn2 zapret <шаг>` печатает changed=0|1 последней строкой, шаг facts - JSON для
шаблонов роли. Переменные стадии приходят окружением процесса. Отбор трафика -
только по метке, которую sing-box ставит на сокеты outbound youtube-ru.
"""

import argparse
import glob
import json
import os
import pwd
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from vpn2 import release, svc

ZAPRET_REF = '038c97e5819df10f8e8c22a2f1fffe57f2f7379b'
ZAPRET_REPO = 'https://github.com/bol-van/zapret2'
# sha256 архива ZAPRET_REF с GitHub, тот же пин zapret2_sha256 в infra/versions.yml.
ZAPRET_SHA256 = 'f31711fa2dc94135bd295542c8024e5db8f6adf71d4c542f7289f3e04347e29d'
# Режет ClientHello. DPI провайдера берёт только split: fake и circular не берут.
SPLIT = '--payload=tls_client_hello --lua-desync=multisplit:pos=10,midsld:seqovl=1'
LUA_MODULES = ('zapret-lib', 'zapret-antidpi', 'zapret-auto')
DIAG_USER = 'ytzdiag'
UNIT_DIR = '/etc/systemd/system'
NFT_CONF = '/etc/nftables-yt-zapret.conf'
MANIFEST = '/tmp/anysda/release-manifest.json'
BUILD_LOG = '/tmp/anysda-nfqws2-build.log'
WHEEL_STAMP = '/opt/vpn2/venv/.vpn2-wheel'
PROBE_STATE = '/var/lib/anysda-yt-failover/state.json'


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
    # Окно клиента в SYN равно wsize << scale; native оставляет масштаб ядра.
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

    Сервер читает окно клиента как window << scale из SYN, а ядро пишет окно под свой
    масштаб: недостающий сдвиг возвращается на каждом исходящем не-SYN пакете.
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
    return (f'        {match} tcp window > {limit} counter tcp window set 65535\n'
            f'        {match} tcp window <= {limit} counter tcp window set tcp window << {shift}')


def source(environ, cfg):
    """Исходники zapret2 на закреплённом коммите из архива выпуска и сборка nfqws2."""
    base, ref, binary = Path(cfg['base']), cfg['ref'], cfg['bin']
    with tempfile.TemporaryDirectory() as work:
        archive = os.path.join(work, 'source.tar.gz')
        # В режиме разработки манифеста выпуска нет: архив сверяется только с закреплённым хешем.
        manifest = ([] if environ.get('VPN2_DEVELOPMENT', 'false') == 'true'
                    else ['--manifest', environ.get('VPN2_RELEASE_MANIFEST') or MANIFEST])
        if release.main([*manifest, 'artifact', 'zapret2-source', archive, '--sha256', ZAPRET_SHA256,
                         '--url', f"{cfg['repo']}/archive/{ref}.tar.gz"]):
            raise StageError('архив zapret2 не получен')
        marker = base / '.release-ref'
        changed = not (marker.is_file() and marker.read_text().strip() == ref)
        if changed:
            base.mkdir(parents=True, exist_ok=True)
            subprocess.run(['tar', '-xzf', archive, '--strip-components=1', '-C', base], check=True)
            marker.write_text(ref + '\n')
    # Никаких `make -q`/`make -n`: цель all зависит от clean с $(MAKE), и сухой прогон стирает binaries/my.
    if changed or not os.access(binary, os.X_OK):
        with open(BUILD_LOG, 'wb') as log:
            if subprocess.run(['make', '-C', base], stdout=log, stderr=subprocess.STDOUT).returncode:
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
    """Значения для скрипта проверки - словами оболочки."""
    return {f'{name}_sh': shlex.quote(str(cfg[name])) for name in ('mark', 'base', 'queue')}


def facts(environ):
    cfg = settings(environ)
    wan = wan_iface()
    diag = pwd.getpwnam(DIAG_USER)
    return {
        'mark': cfg['mark'], 'queue': cfg['queue'], 'base': cfg['base'], 'bin': cfg['bin'],
        'strategy': cfg['strategy'], 'lua_init': lua_init(cfg), 'wan': wan, 'offload': cfg['offload'],
        'diag_uid': diag.pw_uid, 'diag_gid': diag.pw_gid, **shell_values(cfg),
        'window_rules': window_rules(cfg, wan) or '        # (стратегия не меняет масштаб окна — поправка не нужна)',
    }


def restart(cfg):
    """Перезапуск цепочек и nfqws2 только при смене их входов; готовность ждёт ExecStartPost юнита.

    Пока nfqws2 перезапускается, `queue ... bypass` пропускает YouTube мимо десинка.
    """
    changed = svc.restart_if_changed('anysda-yt-nft.service', [f'{UNIT_DIR}/anysda-yt-nft.service', NFT_CONF])
    changed |= svc.restart_if_changed('anysda-yt-nfqws.service', [
        f'{UNIT_DIR}/anysda-yt-nfqws.service', '/usr/local/sbin/anysda-yt-nfqws-ready', cfg['bin'],
        *sorted(glob.glob(f"{cfg['base']}/lua/*.lua"))])
    pid = subprocess.run(['systemctl', 'show', '-p', 'MainPID', '--value', 'anysda-yt-nfqws.service'],
                         capture_output=True, text=True, check=False).stdout.strip()
    # После пересборки живой процесс может держать удалённый inode и не пережить рестарта.
    if pid.isdigit() and pid != '0':
        try:
            stale = os.stat(f'/proc/{pid}/exe').st_ino != os.stat(cfg['bin']).st_ino
        except OSError:
            stale = True
        if stale:
            print('демон на устаревшем inode - рестарт')
            subprocess.run(['systemctl', 'restart', 'anysda-yt-nfqws.service'], check=True)
            changed = True
    return changed


def remeasure():
    """Результат пробы старше её кода: перемерить сразу, а не через 5 минут таймера."""
    try:
        stale = os.stat(WHEEL_STAMP).st_mtime > os.stat(PROBE_STATE).st_mtime
    except OSError:
        stale = os.path.exists(WHEEL_STAMP)
    if stale:
        subprocess.run(['systemctl', 'start', 'anysda-yt-failover.service'], capture_output=True, check=False)
    return False


STEPS = ('facts', 'source', 'restart', 'remeasure')


def main(argv):
    parser = argparse.ArgumentParser(prog='vpn2 zapret')
    parser.add_argument('step', choices=STEPS)
    step = parser.parse_args(argv).step
    try:
        if step == 'facts':
            print(json.dumps(facts(os.environ), ensure_ascii=False, sort_keys=True))
            return 0
        cfg = settings(os.environ)
        changed = {'source': lambda: source(os.environ, cfg), 'restart': lambda: restart(cfg),
                   'remeasure': remeasure}[step]()
    except (StageError, KeyError, OSError, subprocess.CalledProcessError) as error:
        sys.stderr.write(f'vpn2 zapret {step}: {error}\n')
        return 1
    print(f'changed={int(changed)}')
    return 0
