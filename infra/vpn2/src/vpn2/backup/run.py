"""Создание бэкапа: ход anysda-backup.sh (строки 250-577) по блокам, код выхода как у скрипта."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from io import StringIO
from pathlib import Path

from vpn2 import notify
from vpn2.backup import archive, bundle, env

VERSION_URL = 'http://127.0.0.1:51821/api/version'
NO_SOURCE_ROOT = '/opt/anysda-vpn2'
LOCK_WAIT = 60
LOCK_POLL = 0.05
SLICES = 10
JSON_HEADER = 'Content-Type: application/json'
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
    """Пути узла, которые читает скрипт; under(root) переносит их под root (для тестов без root)."""

    env_file: str = env.DEFAULT_ENV_FILE
    secret_file: str = notify.SECRET_FILE
    source_root_file: str = '/etc/anysda/source-root'
    etc_anysda: str = '/etc/anysda'
    runtime_dir: str = '/var/lib/anysda-vpn2/runtime'
    work_parent: str = '/var/lib/anysda-vpn2'
    db: str = '/var/lib/anysda-vpn2/db.sqlite'
    pki_lock: str = '/etc/openvpn/server/.pki.lock'
    etc: str = '/etc'
    wireguard: str = '/etc/wireguard'
    openvpn: str = '/etc/openvpn'
    strongswan: str = '/etc/strongswan'
    swanctl: str = '/etc/swanctl'
    caddy: str = '/var/lib/caddy/.local/share/caddy'
    deploy_state: str = '/var/lib/anysda-vpn2/state'

    @classmethod
    def under(cls, root):
        """Те же пути, но внутри каталога root."""
        return cls(**{name: f'{root}{value}' for name, value in cls().__dict__.items()})


class _Stop(Exception):
    """Ход прерван: code - код выхода скрипта (set -e или явный exit)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _State:
    """FAIL_REASON и WARNING скрипта: причина сбоя и предупреждение для события."""

    def __init__(self):
        self.fail_reason = ''
        self.warning = ''


class _Done:
    def __init__(self, returncode, stdout=b'', stderr=b''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _text(data):
    return data.decode(errors='replace') if isinstance(data, bytes) else (data or '')


def _exec(run, args, err, *, quiet=False, **kwargs):
    """Запуск внешней программы; stderr уходит в err (если не quiet), нет программы - код 127."""
    try:
        done = run(args, capture_output=True, check=False, **kwargs)
    except OSError as exc:
        if not quiet:
            err.write(f'{args[0]}: {exc.strerror}\n')
        return _Done(127)
    if not quiet and done.stderr:
        err.write(_text(done.stderr))
    return done


def _must(run, args, err, **kwargs):
    """Как команда под set -e: код не 0 обрывает ход тем же кодом."""
    done = _exec(run, args, err, **kwargs)
    if done.returncode != 0:
        raise _Stop(done.returncode)
    return done


def _post(run, args, secret_header, body):
    """curl best-effort (`|| true`): секрет заголовка идёт через stdin (-H @-), не в argv."""
    argv = ['curl', '-fsS', '--max-time', '5', '-H', '@-', '-H', JSON_HEADER, '-d', body, *args]
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        run(argv, input=(secret_header + '\n').encode(), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False)


def _secret(paths):
    """Секрет бота, как `$(cat файл)`: без хвостовых переводов строки; нет доступа - None."""
    if not os.access(paths.secret_file, os.R_OK):
        return None
    try:
        return Path(paths.secret_file).read_bytes().decode(errors='surrogateescape').rstrip('\n')
    except OSError:
        return None


def report_failure(rc, state, paths, run):
    """Событие в ленту панели при коде не 0 (4 с предупреждением - backup_warning); как report_failure скрипта."""
    secret = _secret(paths)
    if rc == 0 or secret is None:
        return
    kind, detail = 'backup_failed', state.fail_reason or f'код выхода {rc}'
    if rc == 4 and state.warning:
        kind, detail = 'backup_warning', state.warning
    event = {'type': kind, 'detail': notify.clean_detail(detail)}
    body = json.dumps(event, ensure_ascii=False, separators=(',', ':'))
    _post(run, [notify.PANEL_URL], f'Authorization: Bearer {secret}', body)


def alert_no_passphrase(paths, err):
    """Пароля нет: текст FATAL в err. Событие одно - backup_failed из report_failure.

    Прежний скрипт слал ещё и system_alert боту с английским текстом: владелец получал два
    сообщения об одном сбое.
    """
    err.write(FATAL_TEXT.format(env_file=paths.env_file))


def copy_etc_anysda(bundle_dir, paths, run, err):
    """/etc/anysda целиком; ссылки sso-runtime.json и telegram-runtime.json заменяются файлами из runtime."""
    if not os.path.isdir(paths.etc_anysda):
        return
    target = f'{bundle_dir}/etc-anysda'
    _must(run, ['cp', '-a', paths.etc_anysda, target], err)
    for name in ('sso-runtime.json', 'telegram-runtime.json'):
        if os.path.islink(f'{target}/{name}'):
            os.unlink(f'{target}/{name}')
            runtime = f'{paths.runtime_dir}/{name}'
            if os.path.islink(runtime):
                err.write('backup: runtime must be a regular file\n')
                raise _Stop(1)
            if os.path.isfile(runtime):
                _must(run, ['cp', '-p', runtime, f'{target}/{name}'], err)


@contextlib.contextmanager
def _pki_lock(path, state, wait):
    """Общий замок PKI (flock -s, ждать не дольше wait); нет файла - без замка."""
    if not os.path.exists(path):
        yield
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise _Stop(1) from exc
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    state.fail_reason = 'замок PKI OpenVPN занят дольше минуты'
                    raise _Stop(1) from None
                time.sleep(LOCK_POLL)
        yield
    finally:
        os.close(fd)


def snapshot_access(bundle_dir, paths, state, run, err, wait=LOCK_WAIT):
    """Один срез под замком PKI: db.sqlite, etc-wireguard, etc-openvpn, etc-strongswan, etc-swanctl."""
    names = ('db.sqlite', 'etc-wireguard', 'etc-openvpn', 'etc-strongswan', 'etc-swanctl')
    for name in names:
        target = f'{bundle_dir}/{name}'
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.lexists(target):
            os.unlink(target)
    with _pki_lock(paths.pki_lock, state, wait):
        if os.access(paths.db, os.R_OK):
            _must(run, ['sqlite3', paths.db, '.timeout 10000', f".backup '{bundle_dir}/db.sqlite'"], err)
        else:
            err.write(f'anysda-backup: WARN: {paths.db} не найден — пропускаю db.sqlite\n')
        os.makedirs(f'{bundle_dir}/etc-wireguard', exist_ok=True)
        for name in ('wg0.conf', 'server.priv', 'server.pub'):
            if os.path.isfile(f'{paths.wireguard}/{name}'):
                _must(run, ['cp', '-aL', f'{paths.wireguard}/{name}', f'{bundle_dir}/etc-wireguard/'], err)
        for source, name in ((paths.openvpn, 'etc-openvpn'), (paths.strongswan, 'etc-strongswan'),
                             (paths.swanctl, 'etc-swanctl')):
            if os.path.isdir(source):
                _must(run, ['cp', '-a', source, f'{bundle_dir}/{name}'], err)


def _device_ids(mismatch):
    found = {int(m.group(1)) for line in mismatch.split('\n') if (m := re.match('^устройство ([0-9]+)', line))}
    return sorted(found)


def _mismatch_warning(mismatch, err):
    """Устойчивое расхождение после 10 срезов: сообщение в err и текст предупреждения с номерами устройств."""
    err.write('anysda-backup: WARN: БД и конфиги VPN расходятся в 10 срезах подряд — сохраняю архив как есть; '
              'перевыпусти эти устройства в панели:\n')
    err.write(mismatch + '\n')
    ids = _device_ids(mismatch)
    listed = ', '.join(str(i) for i in ids[:10])
    if len(ids) > 10:
        listed += f' и ещё {len(ids) - 10}'
    return f"БД и конфиги VPN расходятся{f' (устройства: {listed})' if listed else ''} — перевыпусти в панели"


def take_slices(bundle_dir, paths, state, run, err, check, sleep, wait=LOCK_WAIT):
    """Срезы (до 10) со сверкой check; расхождение - снять заново, нечем сверить - предупреждение."""
    for attempt in range(1, SLICES + 1):
        snapshot_access(bundle_dir, paths, state, run, err, wait)
        out = StringIO()
        rc = check(bundle_dir, out, err)
        mismatch = out.getvalue().rstrip('\n')
        if rc == 0:
            return
        if rc == 5:
            err.write(f'anysda-backup: WARN: сверка БД с конфигами VPN не выполнена: {mismatch}\n')
            state.warning = f"сверка БД с конфигами VPN не выполнена: {mismatch.replace(chr(10), '; ')}"
            return
        if rc != 3:
            err.write(f'anysda-backup: WARN: сверка БД с конфигами VPN не выполнена (код {rc})\n')
            state.warning = f'сверка БД с конфигами VPN не выполнена (код {rc})'
            return
        if attempt == SLICES:
            state.warning = _mismatch_warning(mismatch, err)
            return
        err.write(f'anysda-backup: срез {attempt}: БД и конфиги VPN расходятся (идёт выдача или отзыв?), '
                  'снимаю заново:\n')
        err.write(mismatch + '\n')
        sleep(3)


def copy_extras(bundle_dir, paths, run, err):
    """strongswan.conf/.d, ipsec.*, хранилище Caddy без locks, состояние деплоя; сокеты, FIFO и устройства из набора удаляются."""
    for name in ('strongswan.conf', 'strongswan.d', 'ipsec.conf', 'ipsec.secrets'):
        if os.path.exists(f'{paths.etc}/{name}'):
            _must(run, ['cp', '-a', f'{paths.etc}/{name}', f'{bundle_dir}/etc-{name}'], err)
    if os.path.isdir(paths.caddy):
        _must(run, ['cp', '-a', paths.caddy, f'{bundle_dir}/caddy-storage'], err)
        locks = f'{bundle_dir}/caddy-storage/locks'
        if os.path.isdir(locks) and not os.path.islink(locks):
            shutil.rmtree(locks, ignore_errors=True)
        elif os.path.lexists(locks):
            os.unlink(locks)
    # Без mgmt-ips.txt новый entry раздал бы экзитам адреса mesh заново, по позиции в config.yaml.
    if os.path.isdir(paths.deploy_state):
        _must(run, ['cp', '-a', paths.deploy_state, f'{bundle_dir}/deploy-state'], err)
    for root, dirs, files in os.walk(bundle_dir):
        for name in dirs + files:
            full = os.path.join(root, name)
            mode = os.lstat(full).st_mode
            if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
                os.unlink(full)


def _app_version(run):
    """Версия панели с /api/version; любой сбой - unknown."""
    done = _exec(run, ['curl', '-fsS', '--max-time', '10', VERSION_URL], None, quiet=True)
    try:
        if done.returncode != 0:
            return 'unknown'
        return str(json.loads(done.stdout)['version']).rstrip('\n')
    except (ValueError, KeyError, TypeError):
        return 'unknown'


def _git_commit(source_root, run):
    done = _exec(run, ['git', '-C', source_root, 'rev-parse', '--short', 'HEAD'], None, quiet=True)
    return _text(done.stdout).rstrip('\n') if done.returncode == 0 else 'unknown'


def _source_root(paths):
    try:
        text = Path(paths.source_root_file).read_bytes().decode(errors='surrogateescape').rstrip('\n')
    except OSError:
        text = ''
    return text or NO_SOURCE_ROOT


def write_manifest(bundle_dir, source_root, ts, run, err):
    """manifest.json набора: версия панели, коммит, schema_version из среза БД, sha компонентов."""
    schema = '0'
    if os.path.isfile(f'{bundle_dir}/db.sqlite'):
        done = _must(run, ['sqlite3', f'{bundle_dir}/db.sqlite', 'PRAGMA schema_version;'], err)
        schema = _text(done.stdout).rstrip('\n')
    data = bundle.manifest(bundle_dir, version=_app_version(run), git_commit=_git_commit(source_root, run),
                           schema_version=schema, timestamp=ts)
    path = bundle.write_manifest(bundle_dir, data)
    os.chmod(path, 0o600)


def pack_and_encrypt(work, name, passphrase, run, err):
    """tar.gz набора и шифрование age; открытый архив затирается. Возвращает путь зашифрованного файла."""
    plain, encrypted = work / f'{name}.tar.gz', work / f'{name}.tar.gz.age'
    try:
        archive.pack(work, name, plain, run=run)
    except archive.ArchiveError as exc:
        err.write(exc.message + '\n')
        raise _Stop(exc.code) from None
    codes = []

    def _pty(argv, secret, prompts, *, timeout):
        codes.append(archive.pty_run(argv, secret, prompts, timeout=timeout))
        return codes[-1]

    try:
        archive.encrypt(plain, encrypted, passphrase, run=_pty, wiper=lambda path: archive.wipe(path, run=run))
    except archive.ArchiveError as exc:
        if codes and codes[0] != 0:
            raise _Stop(codes[0]) from None
        err.write(exc.message + '\n')
        raise _Stop(exc.code) from None
    return encrypted


def publish(encrypted, final):
    """Зашифрованный архив на место (0600); возвращает размер и sha256."""
    shutil.move(encrypted, final)
    os.chmod(final, 0o600)
    return os.stat(final).st_size, hashlib.sha256(Path(final).read_bytes()).hexdigest()


def _keep(retention):
    try:
        return int(retention)
    except ValueError:
        return 0


def rotate_local(local_dir, final, retention, out):
    """Оставить retention свежих anysda-vpn2-*.tar.gz.age (порядок по mtime, как find|sort -n); final не трогать."""
    found = []
    with os.scandir(local_dir) as entries:
        for entry in entries:
            if fnmatchcase(entry.name, 'anysda-vpn2-*.tar.gz.age') and entry.is_file(follow_symlinks=False):
                found.append((entry.stat(follow_symlinks=False).st_mtime_ns, os.path.join(local_dir, entry.name)))
    paths = [path for _, path in sorted(found)]
    keep = _keep(retention)
    for path in paths[:len(paths) - keep] if len(paths) > keep else []:
        if path == final:
            continue
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        out.write(f'anysda-backup: rotated out {os.path.basename(path)}\n')


def _aws(settings, run, err, *args):
    """aws с ключами в окружении (как AWS_ACCESS_KEY_ID=... aws ...) и регионом из настроек."""
    environ = {**os.environ, 'AWS_DEFAULT_REGION': settings.s3_region,
               'AWS_ACCESS_KEY_ID': settings.s3_access_key, 'AWS_SECRET_ACCESS_KEY': settings.s3_secret_key}
    return _exec(run, ['aws', '--endpoint-url', settings.s3_endpoint, *args], err, env=environ)


def upload_s3(settings, final, key, run, out, err):
    """Выгрузка в S3 и ротация по именам (TS в имени); кроме нового ключа, остаются retention последних."""
    target = f's3://{settings.s3_bucket}'
    out.write(f'anysda-backup: uploading to {target}/{key}\n')
    done = _aws(settings, run, err, 's3', 'cp', final, f'{target}/{key}', '--only-show-errors')
    if done.returncode != 0:
        raise _Stop(done.returncode)
    listing = _aws(settings, run, err, 's3', 'ls', f'{target}/')
    names = [(line.split() or [''])[-1] for line in _text(listing.stdout).splitlines()]
    keys = sorted(n for n in names if re.fullmatch(r'anysda-vpn2-.*\.tar\.gz\.age', n))
    if not keys:
        raise _Stop(listing.returncode or 1)
    keep = _keep(settings.retention)
    old = keys[:len(keys) - keep] if keep > 0 else keys
    for name in old:
        if name == key:
            continue
        done = _aws(settings, run, err, 's3', 'rm', f'{target}/{name}', '--only-show-errors')
        if done.returncode != 0:
            raise _Stop(done.returncode)
        out.write(f'anysda-backup: s3 rotated out {name}\n')
    if listing.returncode != 0:
        raise _Stop(listing.returncode)


def summary(settings, final, size, digest, warning, out):
    """Итоговые строки (printf '%-12s'); с предупреждением - код выхода 4."""
    out.write(f"\n{'backup:':<12} {final}\n{'size:':<12} {size} bytes\n{'hash:':<12} sha256:{digest}\n")
    out.write(f"{'backend:':<12} {settings.backend}\n{'retention:':<12} keep last {settings.retention}\n")
    if warning:
        out.write(f"{'warning:':<12} {warning}\n")
        return 4
    return 0


def _default_check(bundle_dir, out, err):
    """Сверка среза через vpn2.backup.check; check_bundle требует manifest.json, а его ещё нет - ставим пустышку."""
    from vpn2.backup import check
    marker = Path(bundle_dir, bundle.MANIFEST)
    marker.write_text('{}')
    try:
        return check.check_bundle(bundle_dir, out=out, err=err)
    finally:
        marker.unlink(missing_ok=True)


def _make(settings, paths, state, run, out, err, now, check, sleep, wait):
    if not settings.passphrase:
        alert_no_passphrase(paths, err)
        state.fail_reason = 'нет пароля шифрования'
        raise _Stop(2)
    source_root = _source_root(paths)
    os.makedirs(settings.local_dir, exist_ok=True)
    os.chmod(settings.local_dir, 0o700)
    with archive.workdir(paths.work_parent, '.backup-tmp.', run=run) as work:
        ts = env.timestamp(now)
        name = env.bundle_name(ts)
        bundle_dir = work / name
        os.makedirs(bundle_dir)
        copy_etc_anysda(bundle_dir, paths, run, err)
        take_slices(bundle_dir, paths, state, run, err, check, sleep, wait)
        copy_extras(bundle_dir, paths, run, err)
        write_manifest(bundle_dir, source_root, ts, run, err)
        encrypted = pack_and_encrypt(work, name, settings.passphrase, run, err)
        final = f'{settings.local_dir}/{name}.tar.gz.age'
        size, digest = publish(encrypted, final)
        rotate_local(settings.local_dir, final, settings.retention, out)
        if settings.backend == 's3':
            upload_s3(settings, final, f'{name}.tar.gz.age', run, out, err)
        return summary(settings, final, size, digest, state.warning, out)


def create(settings, *, paths=Paths(), run=subprocess.run, out=None, err=None, now=None,
           check=None, sleep=time.sleep, lock_wait=LOCK_WAIT):
    """Снять зашифрованный бэкап узла; код выхода как у anysda-backup.sh (0, 4 с предупреждением, иначе сбой).

    Временный каталог убирается при любом исходе, при коде не 0 уходит событие (report_failure).
    check(каталог, out, err) -> код сверки среза (0, 3, 5...); по умолчанию - vpn2.backup.check.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    check = check or _default_check
    state, rc = _State(), 1
    old_umask = os.umask(0o077)
    try:
        rc = _make(settings, paths, state, run, out, err, now, check, sleep, lock_wait)
    except _Stop as stop:
        rc = stop.code
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        err.write(f'anysda-backup: {type(exc).__name__}: {exc}\n')
        rc = 1
    finally:
        os.umask(old_umask)
        report_failure(rc, state, paths, run)
    return rc
