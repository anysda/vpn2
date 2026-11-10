"""Применение набора при restore: остановка служб, замена путей узла, старт служб и отметка last good.

Повторяет блоки anysda-restore.sh «Остановка сервисов перед swap», «Применение» и «Старт сервисов
обратно»: те же команды с теми же флагами и в том же порядке, те же тексты stdout и stderr. Команда,
сбой которой в скрипте ловит trap ERR, здесь бросает StepFailed с текстом `$BASH_COMMAND`, и блок
`with Rollback(...)` превращает его в откат и код 10. Пути узла - от rb.root, программы - через `run=`.
"""
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from vpn2.backup.rollback import CONTAINERS, CONTROL_DIR, DB_DIR, StepFailed

SYNC_UNITS = ('anysda-ikev2-cert-sync.path', 'anysda-ikev2-cert-sync.timer')
CERT_SYNC = 'anysda-ikev2-cert-sync.service'
STOP_UNITS = ('anysda-failover-watchdog', 'sing-box', 'openvpn-server@server',
              'anysda-ikev2-routing', 'strongswan-starter')
START_UNITS = ('openvpn-server@server', 'anysda-failover-watchdog', 'strongswan-starter',
               'anysda-ikev2-routing')
IKEV2_EXTRA = ('strongswan.conf', 'strongswan.d', 'ipsec.conf', 'ipsec.secrets')
RUNTIME_FILES = ('sso-runtime.json', 'telegram-runtime.json')
_QUIET = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}


@dataclass(frozen=True)
class Paths:
    """Пути узла от корня без `/` в начале; умолчания - из скрипта."""

    caddy_storage: str = 'var/lib/caddy/.local/share/caddy'
    caddy_dirs: tuple = ('var/lib/caddy/.local', 'var/lib/caddy/.local/share')
    svc_lib: str = 'usr/local/lib/anysda/anysda-svc.sh'
    vpn2_apply: str = 'usr/local/sbin/vpn2-apply'


def _print(line):
    print(line, flush=True)


def _say(text, out):
    (out or _print)(f'anysda-restore: {text}')


def _warn(rb, text):
    print(f'anysda-restore: {text}', file=rb.err or sys.stderr, flush=True)


def _rc(run, argv, **kw):
    """Код программы; нет программы или не запустилась - 127, как у bash."""
    try:
        return run(argv, check=False, **kw).returncode
    except OSError:
        return 127


def _ok(run, argv, **kw):
    return _rc(run, argv, **kw) == 0


def _must(run, argv, cause, **kw):
    """Команда под trap ERR: код не 0 - StepFailed(код, текст команды в скрипте)."""
    rc = _rc(run, argv, **kw)
    if rc:
        raise StepFailed(rc, cause)


def stop_services(rb, *, run=subprocess.run, out=None):
    """Остановка писателей перед заменой; остановленные sync-юниты Caddy - в rb (CADDY_SYNC_UNITS)."""
    _say('останавливаю сервисы…', out)
    if os.path.isdir(os.path.join(rb.bundle_dir, 'caddy-storage')):
        for unit in SYNC_UNITS:
            if _ok(run, ['systemctl', 'is-active', '--quiet', unit]):
                rb.stopped(unit)
                _must(run, ['systemctl', 'stop', unit], 'systemctl stop "$unit"')
        if _ok(run, ['systemctl', 'cat', CERT_SYNC], **_QUIET):
            _must(run, ['systemctl', 'stop', CERT_SYNC], f'systemctl stop {CERT_SYNC}')
        _must(run, ['systemctl', 'stop', 'caddy'], 'systemctl stop caddy')
    for unit in STOP_UNITS:
        _rc(run, ['systemctl', 'stop', unit], stderr=subprocess.DEVNULL)
    for name in CONTAINERS:
        if _ok(run, ['docker', 'container', 'inspect', name], **_QUIET):
            _must(run, ['docker', 'stop', name], 'docker stop "$container" > /dev/null',
                  stdout=subprocess.DEVNULL)
        else:
            _must(run, ['docker', 'info'], 'docker info > /dev/null', stdout=subprocess.DEVNULL)


def _db(rb, bundle, run):
    """Блок «установка БД»: новая эпоха control, БД рядом и атомарная замена, WAL/SHM - в WORK."""
    live, db_dir = rb.live, rb.live(DB_DIR)
    rb.touch('db')
    if _ok(run, ['id', 'vpn2-control'], **_QUIET):
        rb.stopped('vpn2-control')
        _must(run, ['systemctl', 'stop', 'vpn2-control.service', 'vpn2-control.socket'],
              'systemctl stop vpn2-control.service vpn2-control.socket')
        if os.path.isdir(live(CONTROL_DIR)):
            _must(run, ['mv', live(CONTROL_DIR), os.path.join(rb.work, 'control-before-restore')],
                  'mv /var/lib/vpn2-control "$WORK/control-before-restore"')
        _must(run, ['install', '-d', '-o', 'vpn2-control', '-g', 'vpn2-control', '-m', '0700',
                    live(CONTROL_DIR)], 'install -d -o vpn2-control -g vpn2-control -m 0700 /var/lib/vpn2-control')
    _must(run, ['mkdir', '-p', db_dir], 'mkdir -p /var/lib/anysda-vpn2')
    tmp, db = os.path.join(db_dir, '.db-restore.sqlite'), os.path.join(db_dir, 'db.sqlite')
    _must(run, ['install', '-m', '600', os.path.join(bundle, 'db.sqlite'), tmp],
          'install -m 600 "$BUNDLE_DIR/db.sqlite" /var/lib/anysda-vpn2/.db-restore.sqlite')
    before = os.path.join(rb.work, 'db-before')
    _must(run, ['mkdir', '-p', before], 'mkdir -p "$WORK/db-before"')
    for suffix in ('wal', 'shm', 'journal'):
        if os.path.exists(f'{db}-{suffix}'):
            _must(run, ['mv', f'{db}-{suffix}', before + '/'],
                  'mv "/var/lib/anysda-vpn2/db.sqlite-$suffix" "$WORK/db-before/"')
    _must(run, ['mv', tmp, db], 'mv /var/lib/anysda-vpn2/.db-restore.sqlite /var/lib/anysda-vpn2/db.sqlite')
    rb.db_swapped = True
    _must(run, ['chown', 'root:root', db], 'chown root:root /var/lib/anysda-vpn2/db.sqlite')


def _wireguard(rb, wg_bundle, run):
    wg = rb.live('etc/wireguard')
    if os.path.isfile(os.path.join(wg_bundle, 'wg0.conf')):
        rb.touch('etc/wireguard')
        _must(run, ['mkdir', '-p', wg], 'mkdir -p /etc/wireguard')
        _must(run, ['cp', '-a', os.path.join(wg_bundle, 'wg0.conf'), f'{wg}/wg0.conf'],
              'cp -a "$BUNDLE_DIR/etc-wireguard/wg0.conf" /etc/wireguard/wg0.conf')
        _must(run, ['chmod', '600', f'{wg}/wg0.conf'], 'chmod 600 /etc/wireguard/wg0.conf')
    if os.path.isfile(os.path.join(wg_bundle, 'server.priv')):
        rb.touch('etc/wireguard')
        for d in (wg, f'{wg}/keys'):
            if not os.path.isdir(d):
                _must(run, ['install', '-d', '-m', '700', d], 'install -d -m 700 "$dir"')
        for key in ('server.priv', 'server.pub'):
            _must(run, ['install', '-m', '600', os.path.join(wg_bundle, key), f'{wg}/keys/{key}'],
                  'install -m 600 "$WG_BUNDLE/$key" "/etc/wireguard/keys/$key"')
            _must(run, ['ln', '-sfn', f'keys/{key}', f'{wg}/{key}'], 'ln -sfn "keys/$key" "/etc/wireguard/$key"')


def _read_ids(path):
    """`while read -r device_id`: только строки с `\\n` в конце, пробелы и табы по краям сняты."""
    with open(path, 'rb') as f:
        lines = f.read().split(b'\n')[:-1]
    return [os.fsdecode(line.replace(b'\0', b'').strip(b' \t')) for line in lines]


def _openvpn(rb, bundle, run):
    rb.touch('etc/openvpn')
    rb.copy_into(os.path.join(bundle, 'etc-openvpn'), rb.live('etc/openvpn'))
    issued = os.path.join(rb.work, 'issued-after-archive')
    if os.path.isfile(issued) and os.path.getsize(issued) > 0:
        for device_id in _read_ids(issued):
            _must(run, ['rm', '-f', rb.live(f'etc/openvpn/server/ccd/dev_{device_id}')],
                  'rm -f "/etc/openvpn/server/ccd/dev_$device_id"')


def _ikev2(rb, bundle, run):
    for name in ('strongswan', 'swanctl'):
        if os.path.isdir(os.path.join(bundle, f'etc-{name}')):
            rb.touch(f'etc/{name}')
            rb.copy_into(os.path.join(bundle, f'etc-{name}'), rb.live(f'etc/{name}'))
    for name in IKEV2_EXTRA:
        src = os.path.join(bundle, f'etc-{name}')
        if os.path.isdir(src):
            rb.touch(f'etc/{name}')
            rb.copy_into(src, rb.live(f'etc/{name}'))
        elif os.path.isfile(src):
            rb.touch(f'etc/{name}')
            _must(run, ['cp', '-a', src, rb.live(f'etc/{name}')], 'cp -a "$BUNDLE_DIR/etc-$name" "/etc/$name"')


def _caddy(rb, bundle, paths, run, out):
    if not os.path.isdir(os.path.join(bundle, 'caddy-storage')):
        _say('Caddy storage отсутствует в архиве — оставляю текущее', out)
        return
    storage = rb.live(paths.caddy_storage)
    rb.touch(paths.caddy_storage, 'etc/anysda')
    _must(run, ['install', '-d', '-m', '700', '-o', 'caddy', '-g', 'caddy', *map(rb.live, paths.caddy_dirs)],
          'install -d -m 700 -o caddy -g caddy /var/lib/caddy/.local /var/lib/caddy/.local/share')
    _must(run, ['rm', '-rf', '--', storage], 'rm -rf -- "$CADDY_STORAGE"')
    _must(run, ['cp', '-a', os.path.join(bundle, 'caddy-storage'), storage],
          'cp -a "$BUNDLE_DIR/caddy-storage" "$CADDY_STORAGE"')
    _must(run, ['rm', '-rf', '--', f'{storage}/locks'], 'rm -rf -- "$CADDY_STORAGE/locks"')
    _must(run, ['chown', '-R', 'caddy:caddy', storage], 'chown -R caddy:caddy "$CADDY_STORAGE"')
    for kind, mode in (('d', '700'), ('f', '600')):
        _must(run, ['find', storage, '-type', kind, '-exec', 'chmod', mode, '{}', '+'],
              f'find "$CADDY_STORAGE" -type {kind} -exec chmod {mode} {{}} +')
    _must(run, ['rm', '-f', rb.live('etc/anysda/ikev2-cert.sha256')], 'rm -f /etc/anysda/ikev2-cert.sha256')
    _say('Caddy storage restored (caddy:caddy, dirs=700 files=600)', out)


def apply_bundle(rb, bundle_dir, *, paths=Paths(), run=subprocess.run, out=None):
    """Замена путей узла из набора в порядке скрипта; путь - в rb.touch ДО замены, отсутствующее - пропуск."""
    bundle = str(bundle_dir)
    rb.step = 'установка БД'
    if os.path.isfile(os.path.join(bundle, 'db.sqlite')):
        _db(rb, bundle, run)
    rb.step = 'восстановление /etc/anysda'
    if os.path.isdir(os.path.join(bundle, 'etc-anysda')):
        rb.touch('etc/anysda')
        _must(run, ['rm', '-f', os.path.join(bundle, 'etc-anysda', 'source-root')],
              'rm -f "$BUNDLE_DIR/etc-anysda/source-root"')
        rb.copy_into(os.path.join(bundle, 'etc-anysda'), rb.live('etc/anysda'))
        for name in ('sso-runtime.json', 'telegram-from-config.json'):
            if not os.path.isfile(os.path.join(bundle, 'etc-anysda', name)):
                _must(run, ['rm', '-f', rb.live(f'etc/anysda/{name}')], f'rm -f /etc/anysda/{name}')
    rb.step = 'восстановление WireGuard'
    _wireguard(rb, os.path.join(bundle, 'etc-wireguard'), run)
    rb.step = 'распаковка PKI OpenVPN'
    if os.path.isdir(os.path.join(bundle, 'etc-openvpn')):
        _openvpn(rb, bundle, run)
    rb.step = 'восстановление IKEv2'
    _ikev2(rb, bundle, run)
    rb.step = 'восстановление хранилища Caddy'
    _caddy(rb, bundle, paths, run, out)


def _panel_runtime(rb, run):
    """Рабочие копии настроек для панели vpn2-panel и ссылки на них из /etc/anysda."""
    rb.touch(f'{DB_DIR}/runtime', 'etc/anysda')
    runtime_dir = rb.live(f'{DB_DIR}/runtime')
    _must(run, ['install', '-d', '-o', 'vpn2-panel', '-g', 'vpn2-panel', '-m', '0700', runtime_dir],
          'install -d -o vpn2-panel -g vpn2-panel -m 0700 /var/lib/anysda-vpn2/runtime')
    for name in RUNTIME_FILES:
        runtime, conf = f'{runtime_dir}/{name}', rb.live(f'etc/anysda/{name}')
        if os.path.isfile(conf) and not os.path.islink(conf):
            _must(run, ['install', '-o', 'vpn2-panel', '-g', 'vpn2-panel', '-m', '0640', conf, runtime],
                  'install -o vpn2-panel -g vpn2-panel -m 0640 "/etc/anysda/$name" "$runtime"')
        elif not os.path.exists(conf):
            _must(run, ['rm', '-f', runtime], 'rm -f "$runtime"')
        _must(run, ['ln', '-sfn', runtime, conf], 'ln -sfn "$runtime" "/etc/anysda/$name"')
    _must(run, ['chown', 'vpn2-panel:vpn2-panel', rb.live(DB_DIR)], 'chown vpn2-panel:vpn2-panel /var/lib/anysda-vpn2')
    if os.path.isfile(rb.live(f'{DB_DIR}/db.sqlite')):
        _must(run, ['chown', '-h', 'vpn2-panel:vpn2-panel', rb.live(f'{DB_DIR}/db.sqlite')],
              'chown -h vpn2-panel:vpn2-panel /var/lib/anysda-vpn2/db.sqlite')


def start_services(rb, *, paths=Paths(), run=subprocess.run, out=None):
    """Старт служб обратно; отказ существующей службы - StepFailed (start_unit идёт через rb.run)."""
    _say('запускаю сервисы…', out)
    rb.step = 'запуск служб'
    rb.start_unit('sing-box')
    if os.path.isfile(rb.live('etc/wireguard/wg0.conf')):
        lib = rb.live(paths.svc_lib)
        if os.access(lib, os.R_OK):
            if not _ok(run, ['bash', '-c', 'source "$1" && wg0_apply', 'anysda-restore', lib]):
                _warn(rb, 'WARN: wg0 не применился')
        else:
            _rc(run, ['systemctl', 'restart', 'wg-quick@wg0'], stderr=subprocess.DEVNULL)
    for unit in START_UNITS:
        rb.start_unit(unit)
    if shutil.which('swanctl'):
        _rc(run, ['swanctl', '--load-all'], **_QUIET)
    if os.path.isdir(os.path.join(rb.bundle_dir, 'caddy-storage')):
        _must(run, ['systemctl', 'start', 'caddy'], 'systemctl start caddy')
        domain = rb.live('etc/anysda/ikev2-domain')
        if os.path.isfile(domain) and os.path.getsize(domain) > 0 \
                and _ok(run, ['systemctl', 'cat', CERT_SYNC], **_QUIET):
            _must(run, ['systemctl', 'start', CERT_SYNC], f'systemctl start {CERT_SYNC}')
        if rb.sync_units:
            _must(run, ['systemctl', 'start', *rb.sync_units], 'systemctl start "${CADDY_SYNC_UNITS[@]}"')
    if _ok(run, ['id', 'vpn2-panel'], **_QUIET):
        _panel_runtime(rb, run)
    if _ok(run, ['id', 'vpn2-control'], **_QUIET):
        _must(run, ['systemctl', 'start', 'vpn2-control.socket', 'vpn2-control.service'],
              'systemctl start vpn2-control.socket vpn2-control.service')
    _rc(run, ['docker', 'start', *CONTAINERS], stderr=subprocess.DEVNULL)


def mark_good(rb, *, paths=Paths(), run=subprocess.run):
    """После блока отката: работающий OpenVPN отмечается last good; отказ - только WARN."""
    tool = rb.live(paths.vpn2_apply)
    if os.access(tool, os.X_OK) and _ok(run, ['systemctl', 'is-active', '--quiet', 'openvpn-server@server']):
        if not _ok(run, [tool, 'mark', 'openvpn']):
            _warn(rb, 'WARN: vpn2-apply mark openvpn не прошёл: last good не совпадает с server.conf')
