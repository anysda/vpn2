"""Создание бэкапа: срез узла, manifest.json, tar.gz, age, ротация, S3. Код выхода 0, 4 (есть предупреждение) или сбой."""
import contextlib
import errno
import fcntl
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vpn2 import fsutil, notify
from vpn2.backup import archive, check, env

VERSION_URL = 'http://127.0.0.1:51821/api/version'
LOCK_WAIT = 60
SLICES = 10
SLICE_NAMES = ('db.sqlite', 'etc-wireguard', 'etc-openvpn', 'etc-strongswan', 'etc-swanctl')
FATAL_TEXT = '''anysda-backup FATAL: ANYSDA_BACKUP_PASSPHRASE отсутствует или пуста.

Ожидается в {env_file}. Источник:
  config.yaml (backup.passphrase)  →  config2env.py  →
  стадия 26-backup рендерит /etc/anysda/backup.env (chmod 600)  →
  source в Bash-скрипте anysda-backup.sh (вручную или из systemd).

Действия:
  1. Запусти ./setup.sh (на оркестраторе) — заполни backup.passphrase.
  2. ./deploy.sh 26-backup ru — стадия пересоздаст {env_file}.
  3. Перезапусти бэкап вручную: /usr/local/bin/anysda-backup.sh
'''


@dataclass(frozen=True)
class Paths:
    """Файл настроек и корень узла (root не пуст только в тестах)."""

    env_file: str = env.DEFAULT_ENV_FILE
    root: str = ''


NO_SPACE = 'нет места на диске'


class Stop(Exception):
    """Ход прерван с кодом выхода code; в событие панели идёт reason целиком либо «шаг: cause»."""

    def __init__(self, code, cause='', reason=''):
        super().__init__(code)
        self.code = code
        self.cause = cause
        self.reason = reason


def _warn(text):
    print(text, file=sys.stderr)


def _must(argv, **kwargs):
    rc, _out, why = archive.call(argv, **kwargs)
    if rc:
        raise Stop(rc, _named(argv[0], rc, why))


def _named(program, rc, why):
    """Причина отказа программы: её последняя строка stderr, а молчала - код выхода."""
    if 'No space left on device' in why:
        return NO_SPACE
    return why or f'{program}: код выхода {rc}'


def _drop(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.unlink(path)


def copy_etc_anysda(bundle, root):
    """/etc/anysda целиком; ссылки sso-runtime.json и telegram-runtime.json заменяются файлами из runtime."""
    if not os.path.isdir(f'{root}/etc/anysda'):
        return
    target = f'{bundle}/etc-anysda'
    _must(['cp', '-a', f'{root}/etc/anysda', target])
    for name in ('sso-runtime.json', 'telegram-runtime.json'):
        if os.path.islink(f'{target}/{name}'):
            os.unlink(f'{target}/{name}')
            runtime = f'{root}/var/lib/anysda-vpn2/runtime/{name}'
            if os.path.islink(runtime):
                _warn('backup: runtime must be a regular file')
                raise Stop(1)
            if os.path.isfile(runtime):
                _must(['cp', '-p', runtime, f'{target}/{name}'])


@contextlib.contextmanager
def _pki_lock(path, wait):
    """Общий замок PKI (как flock -s -w); нет файла - без замка."""
    if not os.path.exists(path):
        yield
        return
    with open(path, 'rb') as lock:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Stop(1, reason='замок PKI OpenVPN занят дольше минуты') from None
                time.sleep(0.05)
        yield


def take_slice(bundle, root, wait=LOCK_WAIT):
    """Один срез под замком PKI: db.sqlite, etc-wireguard, etc-openvpn, etc-strongswan, etc-swanctl."""
    for name in SLICE_NAMES:
        _drop(f'{bundle}/{name}')
    db = f'{root}/var/lib/anysda-vpn2/db.sqlite'
    with _pki_lock(f'{root}/etc/openvpn/server/.pki.lock', wait):
        if os.access(db, os.R_OK):
            _must(['sqlite3', db, '.timeout 10000', f".backup '{bundle}/db.sqlite'"])
        else:
            _warn(f'anysda-backup: WARN: {db} не найден — пропускаю db.sqlite')
        os.makedirs(f'{bundle}/etc-wireguard', exist_ok=True)
        for name in ('wg0.conf', 'server.priv', 'server.pub'):
            if os.path.isfile(f'{root}/etc/wireguard/{name}'):
                _must(['cp', '-aL', f'{root}/etc/wireguard/{name}', f'{bundle}/etc-wireguard/'])
        for name in ('openvpn', 'strongswan', 'swanctl'):
            if os.path.isdir(f'{root}/etc/{name}'):
                _must(['cp', '-a', f'{root}/etc/{name}', f'{bundle}/etc-{name}'])


def check_slice(bundle):
    """Сверка БД среза с конфигами VPN: (код check_bundle, текст расхождений)."""
    out = io.StringIO()
    marker = Path(bundle, archive.MANIFEST)  # check_bundle требует manifest.json, а его ещё нет
    marker.write_text('{}')
    try:
        return check.check_bundle(bundle, out=out), out.getvalue().rstrip('\n')
    finally:
        marker.unlink(missing_ok=True)


def _mismatch_warning(mismatch):
    _warn('anysda-backup: WARN: БД и конфиги VPN расходятся в 10 срезах подряд — сохраняю архив как есть; '
          'перевыпусти эти устройства в панели:')
    _warn(mismatch)
    ids = sorted({int(m.group(1)) for line in mismatch.split('\n') if (m := re.match('^устройство ([0-9]+)', line))})
    listed = ', '.join(map(str, ids[:10])) + (f' и ещё {len(ids) - 10}' if len(ids) > 10 else '')
    return f"БД и конфиги VPN расходятся{f' (устройства: {listed})' if listed else ''} — перевыпусти в панели"


def take_slices(bundle, root, *, check=check_slice, pause=3, wait=LOCK_WAIT):
    """Срезы (до 10) со сверкой; расхождение - снять заново. Возвращает предупреждение или ''."""
    for attempt in range(1, SLICES + 1):
        take_slice(bundle, root, wait)
        rc, mismatch = check(bundle)
        if rc == 0:
            return ''
        if rc != 3:
            warning = 'сверка БД с конфигами VPN не выполнена' + (f': {mismatch}' if rc == 5 else f' (код {rc})')
            _warn(f'anysda-backup: WARN: {warning}')
            return warning.replace('\n', '; ')
        if attempt == SLICES:
            return _mismatch_warning(mismatch)
        _warn(f'anysda-backup: срез {attempt}: БД и конфиги VPN расходятся (идёт выдача или отзыв?), снимаю заново:')
        _warn(mismatch)
        time.sleep(pause)
    return ''


def copy_extras(bundle, root):
    """strongswan.conf/.d, ipsec.*, хранилище Caddy без locks, состояние деплоя; сокеты, FIFO и устройства долой."""
    for name in ('strongswan.conf', 'strongswan.d', 'ipsec.conf', 'ipsec.secrets'):
        if os.path.exists(f'{root}/etc/{name}'):
            _must(['cp', '-a', f'{root}/etc/{name}', f'{bundle}/etc-{name}'])
    caddy = f'{root}/var/lib/caddy/.local/share/caddy'
    if os.path.isdir(caddy):
        _must(['cp', '-a', caddy, f'{bundle}/caddy-storage'])
        _drop(f'{bundle}/caddy-storage/locks')
    # Без mgmt-ips.txt новый entry раздал бы экзитам адреса mesh заново, по позиции в config.yaml.
    if os.path.isdir(f'{root}/var/lib/anysda-vpn2/state'):
        _must(['cp', '-a', f'{root}/var/lib/anysda-vpn2/state', f'{bundle}/deploy-state'])
    for top, dirs, files in os.walk(bundle):
        for name in dirs + files:
            mode = os.lstat(os.path.join(top, name)).st_mode
            if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
                os.unlink(os.path.join(top, name))


def _output(argv):
    """stdout команды без хвостового перевода строки; сбой - None."""
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, text=True)
    except OSError:
        return None
    return done.stdout.rstrip('\n') if done.returncode == 0 else None


def _app_version():
    try:
        return str(json.loads(_output(['curl', '-fsS', '--max-time', '10', VERSION_URL]))['version']).rstrip('\n')
    except (ValueError, KeyError, TypeError):
        return 'unknown'


def write_manifest(bundle, root, ts):
    """manifest.json набора: версия панели, коммит, schema_version из среза БД, sha компонентов."""
    try:
        source = Path(f'{root}/etc/anysda/source-root').read_text(errors='surrogateescape').rstrip('\n')
    except OSError:
        source = ''
    schema = '0'
    if os.path.isfile(f'{bundle}/db.sqlite'):
        schema = _output(['sqlite3', f'{bundle}/db.sqlite', 'PRAGMA schema_version;'])
        if schema is None:
            raise Stop(1)
    data = {'version': _app_version(),
            'git_commit': _output(['git', '-C', source or '/opt/anysda-vpn2', 'rev-parse', '--short', 'HEAD'])
            or 'unknown',
            'schema_version': int(schema),
            'timestamp': ts,
            'components': archive.components(bundle),
            'archive_format': 'tar.gz.age',
            'spec_version': 1}
    fsutil.atomic(Path(bundle, archive.MANIFEST), json.dumps(data, indent=2, ensure_ascii=False) + '\n', mode=0o600)


def rotate_local(local_dir, final, retention):
    """Оставить retention свежих архивов (порядок по mtime); final не трогать."""
    found = sorted((entry.stat(follow_symlinks=False).st_mtime_ns, entry.path) for entry in os.scandir(local_dir)
                   if archive.is_archive(entry))
    for _, path in found[:max(len(found) - retention, 0)]:
        if path != final:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            print(f'anysda-backup: rotated out {os.path.basename(path)}')


def upload_s3(settings, final, key, retention):
    """Выгрузка в S3 и ротация по именам (метка в имени); кроме нового ключа, остаются retention последних."""
    target = f's3://{settings.s3_bucket}'
    print(f'anysda-backup: uploading to {target}/{key}', flush=True)
    rc, _out, why = env.aws(settings, 'cp', final, f'{target}/{key}', '--only-show-errors')
    if rc:
        raise Stop(rc, _named('aws', rc, why))
    listed, text, list_why = env.aws(settings, 'ls', f'{target}/', capture=True)
    names = [(line.split() or [''])[-1] for line in text.splitlines()]
    keys = sorted(name for name in names if re.fullmatch(r'anysda-vpn2-.*\.tar\.gz\.age', name))
    if not keys:
        raise Stop(listed or 1, _named('aws', listed, list_why) if listed else 'архива нет в списке бакета')
    for name in keys[:len(keys) - retention] if retention > 0 else keys:
        if name == key:
            continue
        rc, _out, why = env.aws(settings, 'rm', f'{target}/{name}', '--only-show-errors')
        if rc:
            raise Stop(rc, _named('aws', rc, why))
        print(f'anysda-backup: s3 rotated out {name}')
    if listed:
        raise Stop(listed, _named('aws', listed, list_why))


def _make(settings, paths, enter, check, pause, wait):
    """Ход бэкапа; enter(шаг) - чем кончился бы сбой с этого места, для события панели."""
    if not settings.passphrase:
        sys.stderr.write(FATAL_TEXT.format(env_file=paths.env_file))
        raise Stop(2, reason='нет пароля шифрования')
    root = paths.root
    retention = int(settings.retention) if settings.retention.isdecimal() else 0
    enter('каталог для архива не подготовлен')
    os.makedirs(settings.local_dir, exist_ok=True)
    os.chmod(settings.local_dir, 0o700)
    with archive.workdir(f'{root}/var/lib/anysda-vpn2', '.backup-tmp.') as work:
        ts = datetime.now(UTC).strftime('%Y-%m-%dT%H%M%SZ')
        name = f'anysda-vpn2-{ts}'
        bundle = work / name
        bundle.mkdir()
        enter('копирование /etc/anysda не прошло')
        copy_etc_anysda(bundle, root)
        enter('снимок базы и ключей не снят')
        warning = take_slices(bundle, root, check=check, pause=pause, wait=wait)
        enter('копирование настроек служб не прошло')
        copy_extras(bundle, root)
        enter('описание архива не записано')
        write_manifest(bundle, root, ts)
        final = f'{settings.local_dir}/{name}.tar.gz.age'
        enter('упаковка и шифрование не прошли')
        _must(['tar', '-C', str(work), '-czf', str(work / f'{name}.tar.gz'), name])
        archive.encrypt(work / f'{name}.tar.gz', work / f'{name}.tar.gz.age', settings.passphrase)
        enter('архив не сохранён на диск')
        shutil.move(work / f'{name}.tar.gz.age', final)
        os.chmod(final, 0o600)
        enter('старые архивы не убраны')
        rotate_local(settings.local_dir, final, retention)
        if settings.backend == 's3':
            enter('выгрузка в S3 не прошла')
            upload_s3(settings, final, f'{name}.tar.gz.age', retention)
    print(f"\n{'backup:':<12} {final}\n{'size:':<12} {os.path.getsize(final)} bytes\n"
          f"{'hash:':<12} {archive.sha_file(final)}\n"
          f"{'backend:':<12} {settings.backend}\n{'retention:':<12} keep last {settings.retention}")
    if warning:
        print(f"{'warning:':<12} {warning}")
    return warning


def create(settings, *, paths=Paths(), check=check_slice, pause=3, lock_wait=LOCK_WAIT):
    """Снять зашифрованный бэкап узла; код выхода 0, 4 с предупреждением, иначе сбой.

    Временный каталог убирается при любом исходе, при коде не 0 в журнал панели уходит событие.
    """
    old_umask = os.umask(0o077)
    kind, detail, cause, steps = 'backup_failed', '', '', ['']
    try:
        detail = _make(settings, paths, steps.append, check, pause, lock_wait)
        rc, kind = (4, 'backup_warning') if detail else (0, kind)
    except Stop as stop:
        rc, detail, cause = stop.code, stop.reason, stop.cause
    except archive.ArchiveError as exc:
        _warn(exc.message)
        rc, cause = exc.code, exc.message if exc.code == 3 else f'age: код выхода {exc.code}'
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _warn(f'anysda-backup: {type(exc).__name__}: {exc}')
        rc, cause = 1, _error_cause(exc)
    finally:
        os.umask(old_umask)
    if rc:
        secret_file = f'{paths.root}{notify.SECRET_FILE}'
        if not detail:
            detail = _masked(': '.join(filter(None, (steps[-1], cause or f'код выхода {rc}'))), settings, secret_file)
        notify.panel_event({'type': kind, 'detail': notify.clean_detail(detail)}, secret_file=secret_file)
    return rc


def _error_cause(exc):
    if not isinstance(exc, OSError) or not exc.strerror:
        return str(exc) or type(exc).__name__
    where = f' ({exc.filename})' if exc.filename else ''
    return (NO_SPACE if exc.errno == errno.ENOSPC else exc.strerror) + where


def _masked(text, settings, secret_file):
    """Пароль и ключи в событие не попадают."""
    try:
        panel_secret = Path(secret_file).read_text().strip()
    except OSError:
        panel_secret = ''
    for secret in (settings.passphrase, settings.s3_access_key, settings.s3_secret_key, panel_secret):
        if secret:
            text = text.replace(secret, '***')
    return text
