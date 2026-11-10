"""Подготовка restore до первых изменений на узле (anysda-restore.sh, строки 305-438)."""
import contextlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from vpn2.backup import archive
from vpn2.backup.archive import ArchiveError
from vpn2.backup.errors import RestoreError

CHECKER = '/usr/local/bin/anysda-backup.sh'
LIVE_DB = '/var/lib/anysda-vpn2/db.sqlite'
SNAPSHOT_PATHS = (
    'etc/anysda', 'etc/wireguard', 'etc/openvpn',
    'etc/strongswan', 'etc/swanctl', 'etc/strongswan.conf', 'etc/strongswan.d',
    'etc/ipsec.conf', 'etc/ipsec.secrets',
    'etc/caddy', 'var/lib/caddy/.local/share/caddy',
    'var/lib/anysda-vpn2/runtime', 'var/lib/anysda-vpn2/state', 'var/lib/vpn2-control',
    'opt/AdGuardHome/AdGuardHome.yaml', 'var/anysda/.stamps',
)
_PRIVATE_KEY = re.compile(rb'^[Pp][Rr][Ii][Vv][Aa][Tt][Ee][Kk][Ee][Yy]=')
_INTERFACE = re.compile(rb'^\[[Ii][Nn][Tt][Ee][Rr][Ff][Aa][Cc][Ee]\]$')
_BLANKS = re.compile(rb'[ \t\r\v\f]')


def _stderr(text):
    print(text, file=sys.stderr)


def _shell_checker(path, bundle_dir, run):
    done = run([path, '--check-bundle', str(bundle_dir)], stdout=subprocess.PIPE,
               stderr=subprocess.STDOUT, check=False)
    return done.returncode, done.stdout.decode(errors='replace').rstrip('\n')


def check_bundle_report(bundle_dir, *, checker=CHECKER, run=subprocess.run, out=_stderr):
    """Сверка набора проверяющим; по коду - строки WARN в out (stderr), restore не прерывается.

    checker - путь к скрипту (нет исполняемого файла - сверка пропущена молча) либо функция
    bundle_dir -> (код, вывод). Возвращает код проверки (0, если пропущена).
    """
    if callable(checker):
        code, text = checker(bundle_dir)
    elif os.access(checker, os.X_OK):
        code, text = _shell_checker(checker, bundle_dir, run)
    else:
        return 0
    if code == 5:
        out('anysda-restore: WARN: сверка БД с конфигами VPN не выполнена:')
        out(text)
    elif code not in (0, 3):
        out(f'anysda-restore: WARN: сверка БД с конфигами VPN не выполнена (код {code}):')
        out(text)
    elif code == 3:
        what = []
        for word, name in (('CRL', 'CRL OpenVPN'), ('WireGuard', 'пиры WireGuard'), ('IKEv2', 'учётки IKEv2')):
            if word in text:
                what.append(name)
        out('anysda-restore: WARN: в архиве БД и ' + (', '.join(what) or 'конфиги VPN')
            + ' расходятся — после восстановления перевыпусти эти устройства в панели:')
        out(text)
    return code


def _private_key(conf):
    """Ключ из [Interface] wg0.conf как у awk в скрипте (последний побеждает); пусто - b''."""
    key = b''
    interface = False
    for line in conf.replace(b'\0', b'').split(b'\n'):
        line = _BLANKS.sub(b'', line.split(b'#', 1)[0])
        if line.startswith(b'['):
            interface = bool(_INTERFACE.match(line))
        elif interface and _PRIVATE_KEY.match(line):
            key = line.split(b'=', 1)[1]
    return key


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)


def wg_key_material(bundle_dir, *, run=subprocess.run):
    """Ключ WG в наборе: ссылки сняты, server.priv из wg0.conf, server.pub заново, оба 0600.

    Ключ идёт в `wg pubkey` через stdin (файл), в argv и тексты ошибок не попадает.
    """
    wg_dir = Path(bundle_dir) / 'etc-wireguard'
    conf, priv, pub = wg_dir / 'wg0.conf', wg_dir / 'server.priv', wg_dir / 'server.pub'
    if priv.is_symlink() and not conf.is_file():
        raise RestoreError(5, 'anysda-restore: ссылка на ключ WG без wg0.conf — '
                              'приватный ключ восстановить невозможно')
    for item in (priv, pub):
        if item.is_symlink():
            item.unlink()
    if not (conf.is_file() or priv.is_file()):
        return
    if not (priv.is_file() and priv.stat().st_size > 0):
        try:
            data = _private_key(conf.read_bytes())
        except OSError as exc:
            _write_private(priv, b'')
            raise RestoreError(1, f'anysda-restore: wg0.conf не прочитан: {exc.strerror}') from None
        _write_private(priv, data + b'\n' if data else b'')
    failed = False
    with open(priv, 'rb') as source, open(pub, 'wb') as target:
        os.fchmod(target.fileno(), 0o600)
        try:
            failed = run(['wg', 'pubkey'], stdin=source, stdout=target, check=False).returncode != 0
        except OSError:
            failed = True
    if failed:
        raise RestoreError(5, 'anysda-restore: неверный или отсутствующий приватный ключ WG в архиве')
    os.chmod(priv, 0o600)
    os.chmod(pub, 0o600)


def config_template(source_root, *, dest_dir='/tmp/anysda'):
    """Шаблон панели из установленного дерева в dest_dir (0700, файл 0600); нет шаблона - код 5."""
    template = Path(source_root) / 'infra/configs/anysda-config.yaml.tpl'
    if not os.access(template, os.R_OK):
        raise RestoreError(5, f'anysda-restore: нет шаблона {template} — данные не изменены')
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    target = dest / 'anysda-config.yaml.tpl'
    with contextlib.suppress(FileNotFoundError):
        target.unlink()
    _write_private(target, template.read_bytes())
    return target


def guards(*, force, bundle_dir, db_path=LIVE_DB, run=subprocess.run):
    """Нет пользователя caddy при caddy-storage в наборе - код 5; живая БД с клиентами без force - код 6."""
    if (Path(bundle_dir) / 'caddy-storage').is_dir():
        try:
            missing = run(['id', 'caddy'], capture_output=True, check=False).returncode != 0
        except OSError:
            missing = True
        if missing:
            raise RestoreError(5, 'anysda-restore: нет пользователя caddy — сначала выполни деплой '
                                  'без panel.domain; данные не изменены')
    if not os.path.isfile(db_path):
        return
    size = os.path.getsize(db_path)
    try:
        done = run(['sqlite3', str(db_path), 'SELECT COUNT(*) FROM clients;'],
                   capture_output=True, check=False)
        text = done.stdout.decode(errors='replace').strip() if done.returncode == 0 else '0'
    except OSError:
        text = '0'
    clients = int(text) if text.isdigit() else 0
    if size > 0 and clients > 0 and not force:
        raise RestoreError(6, f'anysda-restore: ABORT — текущая db.sqlite не пустая ({clients} клиентов, {size} байт).\n'
                              'Будут перетёрты: db.sqlite, /etc/anysda/*, /etc/wireguard/wg0.conf, /etc/openvpn/*, '
                              'конфиги и ключи IKEv2, хранилище Caddy (если есть в архиве)\n'
                              'Запусти повторно с --force чтобы согласиться.')


def _must(run, args, **kwargs):
    try:
        done = run(args, capture_output=True, check=False, **kwargs)
    except OSError as exc:
        raise RestoreError(127, f'anysda-restore: {args[0]} не запущен: {exc.strerror}') from None
    if done.returncode != 0:
        raise RestoreError(done.returncode, f'anysda-restore: {args[0]} завершился с кодом {done.returncode}')


def pre_restore_snapshot(settings, ts, *, work, paths=SNAPSHOT_PATHS, root='/', live_db=LIVE_DB,
                         run=subprocess.run, pty=archive.pty_run, out=print):
    """Страховка .pre-restore/pre-restore-<ts>.tar.gz.age до остановки служб; вернуть путь.

    work - рабочий каталог restore (в скрипте $WORK). Любая ошибка - RestoreError до изменений узла.
    """
    snap_dir = Path(settings.local_dir) / '.pre-restore'
    snap_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(snap_dir, 0o700)
    work = Path(work)
    tar_path = work / f'pre-restore-{ts}.tar.gz'
    final = snap_dir / f'pre-restore-{ts}.tar.gz.age'
    out(f'anysda-restore: pre-restore snapshot → {final}')
    tree = work / 'pre-restore'
    (tree / 'var/lib/anysda-vpn2').mkdir(parents=True, exist_ok=True)
    if os.path.isfile(live_db):
        _must(run, ['sqlite3', str(live_db), '.timeout 10000',
                    f".backup '{tree / 'var/lib/anysda-vpn2/db.sqlite'}'"])
    for path in paths:
        if os.path.exists(Path(root) / path) or os.path.islink(Path(root) / path):
            _must(run, ['cp', '-a', '--parents', path, f'{tree}/'], cwd=root)
    _must(run, ['find', str(tree), '(', '-type', 's', '-o', '-type', 'p', '-o', '-type', 'b',
                '-o', '-type', 'c', ')', '-delete'])
    try:
        archive.pack(tree, '.', tar_path, run=run)
        encrypted = work / 'pre-restore.tar.gz.age'
        try:
            archive.encrypt(tar_path, encrypted, settings.passphrase, run=pty)
        except ArchiveError:
            raise RestoreError(5, 'anysda-restore: пустой страховочный архив') from None
    except ArchiveError as exc:
        raise RestoreError(exc.code, exc.message) from None
    shutil.move(encrypted, final)
    os.chmod(final, 0o600)
    out(f'anysda-restore: pre-restore snapshot saved ({final.stat().st_size} bytes)')
    return final
