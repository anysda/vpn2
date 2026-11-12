"""Восстановление узла из архива: выбор архива, проверки, замена данных под откатом, стадии и итог.

Коды выхода: 2 настройки и аргументы, 3 нет архива, 4 расшифровка, 5 архив или узел не годятся,
6 живая база без --force, 7 99-verify, 8 пароли не пересинхронизированы, 9 несовместимая версия,
10 откат после сбоя посреди замены.

`--preflight` - проход деплоя перед открытием выпуска: все отказы до первого изменения узла теми же
кодом и строкой, службы и данные не тронуты.
"""
import contextlib
import glob
import hashlib
import io
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vpn2 import wg
from vpn2.backup import archive, check, env, revoke
from vpn2.backup import run as run_backup
from vpn2.fsutil import filled
from vpn2.release.state import migration_history

NO_PASSPHRASE = ('anysda-restore FATAL: ANYSDA_BACKUP_PASSPHRASE пуст в {env_file}.\n'
                 '  Заполни backup.passphrase в config.yaml и прогони ./deploy.sh 26-backup ru')
FLAGS = {'--force': 'force', '-f': 'force', '--dry-run': 'dry_run', '--allow-incompatible': 'allow_incompatible',
         '--preflight': 'preflight'}
CONTAINER_MIGRATIONS = 'anysda-vpn2:/app/server/database/migrations'
JOURNAL = 'meta/_journal.json'
SNAPSHOT_PATHS = (
    'etc/anysda', 'etc/wireguard', 'etc/openvpn',
    'etc/strongswan', 'etc/swanctl', 'etc/strongswan.conf', 'etc/strongswan.d',
    'etc/ipsec.conf', 'etc/ipsec.secrets',
    'etc/caddy', 'var/lib/caddy/.local/share/caddy',
    'var/lib/anysda-vpn2/runtime', 'var/lib/anysda-vpn2/state', 'var/lib/vpn2-control',
    'opt/AdGuardHome/AdGuardHome.yaml', 'var/anysda/.stamps',
)
DB_DIR = 'var/lib/anysda-vpn2'
DB = f'{DB_DIR}/db.sqlite'
CONTROL_DIR = 'var/lib/vpn2-control'
CONTROL_UNITS = ('vpn2-control.socket', 'vpn2-control.service')
CADDY_STORAGE = 'var/lib/caddy/.local/share/caddy'
CONTAINERS = ('anysda-vpn2', 'anysda-tgbot')
SYNC_UNITS = ('anysda-ikev2-cert-sync.path', 'anysda-ikev2-cert-sync.timer')
CERT_SYNC = 'anysda-ikev2-cert-sync.service'
STOP_UNITS = ('anysda-failover-watchdog', 'sing-box', 'openvpn-server@server',
              'anysda-ikev2-routing', 'strongswan-starter')
START_UNITS = ('openvpn-server@server', 'anysda-failover-watchdog', 'strongswan-starter', 'anysda-ikev2-routing')
ROLLBACK_UNITS = ('sing-box', *START_UNITS)
# Шаги стадий 22, 30, 35, что читают восстановленные пароли; установку ведут их роли.
STAGES = {'22-adguard': ('adguard config', 'adguard restart'), '30-frontend': ('containers panel',),
          '35-telegram': ('containers telegram',)}
RESYNC = ('set -a; source "$1"; set +a; r=$2/infra; shift 2; '
          'export VPN2_POOLS=$r/../app/shared/vpn-pools.json VPN2_PANEL_CONFIG_TPL=$r/configs/anysda-config.yaml.tpl; '
          'for step in "$@"; do ${VPN2_BIN:-/opt/vpn2/venv/bin/vpn2} $step || exit; done')
# Проверка узла: окружение стадий целиком, пакет из дерева, интерпретатор тот же, что у restore.
VERIFY = 'set -a; source "$1"; set +a; PYTHONPATH=$2 exec "$3" -B -m vpn2.cli verify'
QUIET = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
SIGNALS = {signal.SIGINT: (130, 'получен сигнал INT'), signal.SIGTERM: (143, 'получен сигнал TERM')}


RestoreError = archive.ArchiveError


class StepFailed(Exception):
    """Команда шага замены вернула не 0: код и сама команда."""

    def __init__(self, rc, cause):
        super().__init__(cause)
        self.rc = rc
        self.cause = cause


class _Signalled(BaseException):
    """INT или TERM посреди замены: код и причина для отката."""

    __init__ = StepFailed.__init__


def _say(text):
    print(f'anysda-restore: {text}', flush=True)


def _warn(text):
    print(f'anysda-restore: {text}', file=sys.stderr, flush=True)


def _write_private(path, data):
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as handle:
        handle.write(data)


# Выбор архива

@dataclass(frozen=True)
class Args:
    """Аргументы restore: имя архива (или latest) и флаги."""

    name: str = 'latest'
    force: bool = False
    dry_run: bool = False
    allow_incompatible: bool = False
    preflight: bool = False


def parse_args(argv):
    """Имя необязательно и только первым, дальше флаги в любом порядке."""
    argv = list(argv)
    name = argv.pop(0) if argv and argv[0] and not argv[0].startswith('-') else 'latest'
    if unknown := next((arg for arg in argv if arg not in FLAGS), None):
        raise RestoreError(2, f'anysda-restore: неизвестный аргумент: {unknown}')
    return Args(name=name, **{FLAGS[arg]: True for arg in argv})


def archive_ts(name):
    """Метка создания из имени архива или None; mtime не годится - меняется при копировании."""
    match = archive.NAME.fullmatch(name)
    return match.group(1) if match else None


def age_text(seconds):
    """Возраст словами: 'N д M ч', 'N ч M мин' или 'N мин'."""
    if seconds >= 86400:
        return f'{seconds // 86400} д {seconds % 86400 // 3600} ч'
    if seconds >= 3600:
        return f'{seconds // 3600} ч {seconds % 3600 // 60} мин'
    return f'{int(seconds / 60)} мин'


def _newest(names):
    return max((name for name in names if archive_ts(name)), key=archive_ts, default=None)


def resolve_latest(settings):
    """Самый свежий по метке архив среди локальных и S3: (имя, 'local' | 'S3').

    Недоступный S3 - предупреждение и выбор среди локальных; пусто везде - RestoreError(3).
    """
    try:
        local = _newest(e.name for e in os.scandir(settings.local_dir) if e.is_file(follow_symlinks=False))
    except OSError:
        local = None
    cloud = None
    if settings.backend == 's3':
        code, rows, why = env.s3_names(settings)
        if code == 0:
            cloud = _newest(row[0] for row in rows)
        else:
            _warn('WARN: S3 недоступен — выбираю только среди локальных архивов, '
                  f'в облаке может лежать более свежий:\n  {why}')
    if local is None and cloud is None:
        raise RestoreError(3, f"anysda-restore: ни одного backup'а ни в {settings.local_dir}, ни в S3")
    pick, source, other, other_from = local, 'local', None, ''
    if cloud is not None and (local is None or archive_ts(cloud) > archive_ts(local)):
        pick, source, other, other_from = cloud, 'S3', local, 'local'
    elif cloud is not None and cloud != local:
        other, other_from = cloud, 'S3'
    created = archive.stamp_epoch(archive_ts(pick))
    _warn(f'latest = {pick} ({source}), создан {archive_ts(pick)}, {age_text(int(time.time()) - created)} назад')
    if other:
        gap = age_text(created - archive.stamp_epoch(archive_ts(other)))
        _warn(f'  самый свежий {other_from}: {other} — старше выбранного на {gap}')
    return pick, source


def _pick(args, settings):
    """Имя архива и откуда он: latest - самый свежий, иначе имя из аргумента (каталог отбрасывается)."""
    if args.name == 'latest':
        return resolve_latest(settings)
    name = os.path.basename(args.name)
    if os.path.isfile(os.path.join(settings.local_dir, name)):
        return name, 'local'
    if settings.backend != 's3':
        raise RestoreError(3, f'anysda-restore: архив {name} не найден в {settings.local_dir}')
    return name, 'S3'


def s3_pull(settings, name):
    """Скачать архив из S3 в локальный каталог с правами 0600."""
    os.makedirs(settings.local_dir, mode=0o700, exist_ok=True)
    target = os.path.join(settings.local_dir, name)
    code, _out, why = env.s3(settings, 'copyto', f's3:{name}', target)
    if why.endswith('parameter null or not set'):
        raise RestoreError(1, f'anysda-restore: {why}')
    try:
        if code == 0:
            os.chmod(target, 0o600)
    except OSError:
        code = 1
    if code:
        raise RestoreError(3, f'anysda-restore: {name} не скачан из S3')


# Проверки архива до любых изменений

def _open(archive_path, work, passphrase):
    """Расшифровать и распаковать архив в work; вернуть каталог набора anysda-vpn2-*."""
    plain = work / 'restore.tar.gz'
    archive.decrypt(archive_path, plain, passphrase)
    archive.unpack(plain, work)
    archive.wipe(plain)
    found = sorted(path for path in glob.glob(os.path.join(glob.escape(str(work)), 'anysda-vpn2-*'))
                   if os.path.isdir(path) and not os.path.islink(path))
    if not found:
        raise RestoreError(5, 'anysda-restore: в архиве нет ожидаемой папки anysda-vpn2-*')
    return found[0]


def check_manifest(bundle, archive_name=None):
    """Сверить компоненты набора с manifest.json и вернуть манифест; отказ - RestoreError(5).

    Лишние файлы вне манифеста не ошибка; имя архива не по manifest.timestamp - предупреждение.
    """
    path = os.path.join(bundle, archive.MANIFEST)
    if not os.access(path, os.R_OK):
        raise RestoreError(5, 'anysda-restore: manifest.json отсутствует — corrupted архив')
    try:
        with open(path) as f:
            manifest = json.load(f)
        components = dict(manifest['components'])
    except (ValueError, KeyError, AttributeError, TypeError, OSError) as exc:
        raise RestoreError(5, f'anysda-restore: manifest.json не разобран: {type(exc).__name__}: {exc}') from exc
    stamp = manifest.get('timestamp')
    if archive_name is not None and archive_name != f'anysda-vpn2-{stamp}.tar.gz.age':
        _warn(f'WARN: имя архива не совпадает с manifest.timestamp={stamp}')
    fail = []
    for name, expected in components.items():
        full = os.path.join(bundle, name)
        if not os.path.exists(full):
            fail.append(f'  {name}: missing')
        elif (actual := archive.sha(full)) != expected:
            fail.append(f'  {name}: expected {expected}, got {actual}')
    if fail:
        raise RestoreError(5, '\n'.join(['manifest verification FAILED:', *fail]))
    print(f'manifest verified: {len(components)} components,'
          f' archive={stamp} app_v={manifest.get("version")} git={manifest.get("git_commit")}', flush=True)
    return manifest


def _find_journal(work, source_root):
    """Каталог миграций: сначала из контейнера панели (docker cp), запасной - клон исходников."""
    copied = os.path.join(work, 'migrations')
    if archive.code(['docker', 'cp', CONTAINER_MIGRATIONS, copied], **QUIET) == 0 \
            and os.access(os.path.join(copied, JOURNAL), os.R_OK):
        return copied
    source = os.path.join(source_root, 'app/server/database/migrations')
    return source if os.access(os.path.join(source, JOURNAL), os.R_OK) else None


def _schema(database, folder):
    """Строка о схеме БД архива; история миграций не подходит текущей версии - ValueError."""
    with open(os.path.join(folder, JOURNAL)) as f:
        entries = json.load(f)['entries']
    journal = [(int(e['when']), hashlib.sha256(Path(folder, e['tag'] + '.sql').read_bytes()).hexdigest())
               for e in entries]
    history = migration_history(database)
    if history is None:
        raise ValueError('в базе архива нет истории миграций (__drizzle_migrations)')
    history = [(int(when), digest) for when, digest in history]
    if len(history) > len(journal) and history[:len(journal)] == journal:
        raise ValueError(f'схема БД архива новее текущей версии: {len(history)} миграций против {len(journal)}')
    for index, row in enumerate(history):
        if index >= len(journal) or row != journal[index]:
            name = entries[index]['tag'] if index < len(journal) else f'#{index + 1}'
            raise ValueError(f'история миграций архива расходится с текущей версией на {name}')
    behind = len(journal) - len(history)
    return (f'схема БД архива: {len(history)} из {len(journal)} миграций текущей версии'
            + (f', {behind} догонит стадия 30' if behind else ''))


def check_compat(manifest, bundle, work, source_root, allow=False):
    """Формат архива (spec_version) и схема БД против журнала drizzle текущей версии.

    Несовместимость - RestoreError(9); при allow - только предупреждение и работа дальше.
    """
    def incompatible(text):
        if not allow:
            raise RestoreError(9, f'anysda-restore: ABORT — {text}; данные не изменены (обход: --allow-incompatible)')
        _warn(f'WARN: {text} — продолжаю по --allow-incompatible')

    spec = manifest.get('spec_version', 1)
    if str(spec) != '1':
        incompatible(f'формат архива spec_version={spec}, этот restore понимает только 1')
    database = os.path.join(bundle, 'db.sqlite')
    if not os.path.exists(database):
        return
    folder = _find_journal(work, source_root)
    if folder is None:
        return incompatible('не найден журнал миграций текущей версии '
                            f'(ни в контейнере anysda-vpn2, ни в {source_root})')
    try:
        _say(_schema(database, folder))
    except Exception as exc:  # noqa: BLE001 - любой отказ сверки схемы означает несовместимость
        incompatible(str(exc) if isinstance(exc, ValueError) else f'{type(exc).__name__}: {exc}')


def check_bundle_report(bundle):
    """Сверка БД набора с его конфигами VPN: только строки WARN, restore не прерывается."""
    lines = io.StringIO()
    code = check.check_bundle(bundle, out=lines, err=lines)
    text = lines.getvalue().rstrip('\n')
    if code == 0:
        return
    if code == 3:
        what = [name for word, name in (('CRL', 'CRL OpenVPN'), ('WireGuard', 'пиры WireGuard'),
                                        ('IKEv2', 'учётки IKEv2')) if word in text]
        _warn('WARN: в архиве БД и ' + (', '.join(what) or 'конфиги VPN')
              + ' расходятся — после восстановления перевыпусти эти устройства в панели:')
    else:
        _warn('WARN: сверка БД с конфигами VPN не выполнена' + (':' if code == 5 else f' (код {code}):'))
    print(text, file=sys.stderr, flush=True)


def wg_key_material(bundle):
    """Ключ WG в наборе: ссылки сняты, server.priv из wg0.conf, server.pub заново, оба 0600.

    Ключ идёт в `wg pubkey` через stdin (файл), в argv и тексты ошибок не попадает.
    """
    wg_dir = Path(bundle) / 'etc-wireguard'
    conf, priv, pub = wg_dir / 'wg0.conf', wg_dir / 'server.priv', wg_dir / 'server.pub'
    if priv.is_symlink() and not conf.is_file():
        raise RestoreError(5, 'anysda-restore: ссылка на ключ WG без wg0.conf — '
                              'приватный ключ восстановить невозможно')
    for item in (priv, pub):
        if item.is_symlink():
            item.unlink()
    if not (conf.is_file() or priv.is_file()):
        return
    if not filled(priv):
        try:
            key = wg.private_key(conf.read_bytes().decode('latin-1'))
        except OSError as exc:
            raise RestoreError(1, f'anysda-restore: wg0.conf не прочитан: {exc.strerror}') from None
        _write_private(priv, key.encode('latin-1') + b'\n' if key else b'')
    with open(priv, 'rb') as source:
        _write_private(pub, b'')
        with open(pub, 'wb') as target:
            failed = archive.code(['wg', 'pubkey'], stdin=source, stdout=target) != 0
    if failed:
        raise RestoreError(5, 'anysda-restore: неверный или отсутствующий приватный ключ WG в архиве')
    os.chmod(priv, 0o600)


def config_template(source_root, dest_dir):
    """Шаблон панели из установленного дерева в dest_dir (0700, файл 0600); нет шаблона - код 5."""
    template = Path(source_root) / 'infra/configs/anysda-config.yaml.tpl'
    if not os.access(template, os.R_OK):
        raise RestoreError(5, f'anysda-restore: нет шаблона {template} — данные не изменены')
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    (dest / 'anysda-config.yaml.tpl').unlink(missing_ok=True)
    _write_private(dest / 'anysda-config.yaml.tpl', template.read_bytes())


def guards(bundle, live_db, force):
    """Нет пользователя caddy при caddy-storage в наборе - код 5; живая БД с клиентами без force - код 6."""
    if os.path.isdir(os.path.join(bundle, 'caddy-storage')) and archive.code(['id', 'caddy'], capture_output=True):
        raise RestoreError(5, 'anysda-restore: нет пользователя caddy — сначала выполни деплой '
                              'без panel.domain; данные не изменены')
    if not os.path.isfile(live_db):
        return
    size = os.path.getsize(live_db)
    try:
        with contextlib.closing(sqlite3.connect(f'file:{live_db}?mode=ro', uri=True, timeout=10)) as db:
            clients = db.execute('SELECT COUNT(*) FROM clients').fetchone()[0]
    except sqlite3.Error:
        clients = 0
    if size > 0 and clients > 0 and not force:
        raise RestoreError(6, f'anysda-restore: ABORT — текущая db.sqlite не пустая ({clients} клиентов, {size} байт).\n'
                              'Будут перетёрты: db.sqlite, /etc/anysda/*, /etc/wireguard/wg0.conf, /etc/openvpn/*, '
                              'конфиги и ключи IKEv2, хранилище Caddy (если есть в архиве)\n'
                              'Запусти повторно с --force чтобы согласиться.')


def pre_restore_snapshot(settings, work, root, live_db):
    """Страховка .pre-restore/pre-restore-<метка>.tar.gz.age до остановки служб; вернуть путь.

    Любая ошибка - RestoreError до изменений узла.
    """
    def must(argv, **kwargs):
        if rc := archive.code(argv, capture_output=True, **kwargs):
            raise RestoreError(rc, f'anysda-restore: {argv[0]} завершился с кодом {rc}')

    now = datetime.now(UTC)
    stamp = now.strftime('%Y-%m-%dT%H%M%S.') + f'{now.microsecond * 1000:09d}Z'
    snap_dir = Path(settings.local_dir) / '.pre-restore'
    snap_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(snap_dir, 0o700)
    final = snap_dir / f'pre-restore-{stamp}.tar.gz.age'
    _say(f'pre-restore snapshot → {final}')
    tree = work / 'pre-restore'
    (tree / DB_DIR).mkdir(parents=True, exist_ok=True)
    if os.path.isfile(live_db):
        try:
            run_backup.db_copy(live_db, tree / DB)
        except sqlite3.Error as exc:
            raise RestoreError(1, f'anysda-restore: sqlite3: {exc}') from None
    for path in SNAPSHOT_PATHS:
        if os.path.lexists(os.path.join(root, path)):
            must(['cp', '-a', '--parents', path, f'{tree}/'], cwd=root)
    archive.drop_special_files(tree)
    tar_path, encrypted = work / 'pre-restore.tar.gz', work / 'pre-restore.tar.gz.age'
    archive.pack(tree, '.', tar_path)
    try:
        archive.encrypt(tar_path, encrypted, settings.passphrase)
    except archive.ArchiveError:
        raise RestoreError(5, 'anysda-restore: пустой страховочный архив') from None
    shutil.move(encrypted, final)
    os.chmod(final, 0o600)
    _say(f'pre-restore snapshot saved ({final.stat().st_size} bytes)')
    return final


# Замена данных под откатом

def _raise_signalled(signum, _frame):
    raise _Signalled(*SIGNALS[signum])


def _hold(_signum, _frame):
    """Сигнал посреди отката его не прерывает (обработчик-функция: дети его не наследуют)."""


class Rollback:
    """Замена данных узла после остановки служб: исключение или INT/TERM в блоке - откат и код 10.

    touched - заменённые пути от корня (или `db`), root - корень узла, run - запуск программ.
    """

    def __init__(self, *, presnap_root, presnap_final, work, bundle, root='/', run=subprocess.run):
        self.presnap_root, self.presnap_final = str(presnap_root), str(presnap_final)
        self.work, self.bundle, self.root, self.run = str(work), str(bundle), str(root), run
        self.db_owner, self.db_mode = '0:0', '600'
        if os.path.isfile(self.live(DB)):
            info = os.stat(self.live(DB))
            self.db_owner, self.db_mode = f'{info.st_uid}:{info.st_gid}', format(info.st_mode & 0o7777, 'o')
        self.step = 'остановка служб'
        self.touched, self.sync_units = [], []
        self.db_swapped = self.control_stopped = False
        self._saved = {}

    def live(self, rel):
        """Путь узла `/rel` под корнем root."""
        return os.path.join(self.root, rel)

    def code(self, argv, **kwargs):
        try:
            return self.run(argv, check=False, **kwargs).returncode
        except OSError:
            return 127

    def ok(self, argv, **kwargs):
        return self.code(argv, **kwargs) == 0

    def must(self, argv, **kwargs):
        """Команда шага: код не 0 - StepFailed и откат."""
        if rc := self.code(argv, **kwargs):
            raise StepFailed(rc, shlex.join(argv))

    def exists(self, unit):
        return self.ok(['systemctl', 'cat', unit], **QUIET)

    def copy_into(self, src, dst):
        """`cp -a src/. dst/`, у живого dst прежние владелец и режим."""
        owner = os.stat(dst) if os.path.isdir(dst) else None
        os.makedirs(dst, exist_ok=True)
        self.must(['cp', '-a', f'{src}/.', f'{dst}/'])
        if owner:
            os.chown(dst, owner.st_uid, owner.st_gid)
            os.chmod(dst, owner.st_mode & 0o7777)

    def start_unit(self, unit):
        """Запуск существующей службы; отказ - StepFailed."""
        self.step = f'запуск службы {unit}'
        if self.exists(unit):
            self.must(['systemctl', 'start', unit])

    def __enter__(self):
        self._saved = {s: signal.signal(s, _raise_signalled) for s in SIGNALS}
        return self

    def __exit__(self, _exc_type, exc, _tb):
        for s in SIGNALS:
            signal.signal(s, _hold)
        try:
            if not isinstance(exc, (Exception, _Signalled)):
                return False
            rc, cause = (exc.rc, exc.cause) if isinstance(exc, (StepFailed, _Signalled)) else (1, str(exc))
            self.rollback(rc, cause)
        finally:
            for s, handler in self._saved.items():
                signal.signal(s, handler)
        raise RestoreError(10, f'откат после сбоя на шаге «{self.step}»') from exc

    def _path_back(self, rel):
        """`/rel` заново из страховочной копии; не было там - только удалить."""
        if not self.ok(['rm', '-rf', '--', self.live(rel)]):
            return False
        saved = os.path.join(self.presnap_root, rel)
        if not os.path.lexists(saved):
            return True
        parent = self.live(os.path.dirname(rel))
        os.makedirs(parent, exist_ok=True)
        return self.ok(['cp', '-a', saved, parent + '/'])

    def _db_back(self):
        if not self.db_swapped:
            if os.path.isdir(os.path.join(self.work, 'db-before')):
                with contextlib.suppress(StepFailed, OSError):
                    self.copy_into(os.path.join(self.work, 'db-before'), self.live(DB_DIR))
            return self.ok(['rm', '-f', self.live(f'{DB_DIR}/.db-restore.sqlite')])
        saved, db = os.path.join(self.presnap_root, DB), self.live(DB)
        if not os.path.isfile(saved):
            return self.ok(['rm', '-f', db])
        uid, _, gid = self.db_owner.partition(':')
        tmp = self.live(f'{DB_DIR}/.db-rollback.sqlite')
        return (self.ok(['install', '-m', self.db_mode, '-o', uid, '-g', gid, saved, tmp])
                and self.ok(['rm', '-f', f'{db}-wal', f'{db}-shm', f'{db}-journal'])
                and self.ok(['mv', tmp, db]))

    def _control_back(self):
        before = os.path.join(self.work, 'control-before-restore')
        return self.ok(['rm', '-rf', self.live(CONTROL_DIR)]) and self.ok(['mv', before, self.live(CONTROL_DIR)])

    def rollback(self, rc, cause):
        """Вернуть заменённое, запустить службы, отчитаться в stderr; каждый шаг идёт, даже если прежний не удался."""
        def safe(step, *args):
            try:
                return step(*args)
            except Exception:  # noqa: BLE001 - сбой шага отката только отметка, откат идёт дальше
                return False

        back, failed = [], []
        _warn(f'FAILED на шаге «{self.step}»: {cause} (код {rc}) — возвращаю прежнее состояние')
        for item in dict.fromkeys(self.touched):
            if item != 'db':
                (back if safe(self._path_back, item) else failed).append(f'/{item}')
                continue
            (back if safe(self._db_back) else failed).append(f'/{DB}')
            if os.path.isdir(os.path.join(self.work, 'control-before-restore')):
                (back if safe(self._control_back) else failed).append(f'/{CONTROL_DIR}')
        down = [unit for unit in ROLLBACK_UNITS
                if self.exists(unit) and not self.ok(['systemctl', 'start', unit], **QUIET)]
        if os.path.isfile(self.live('etc/wireguard/wg0.conf')) \
                and not self.ok(['systemctl', 'restart', 'wg-quick@wg0'], **QUIET):
            down.append('wg-quick@wg0')
        if shutil.which('swanctl'):
            self.ok(['swanctl', '--load-all'], **QUIET)
        if os.path.isdir(os.path.join(self.bundle, 'caddy-storage')):
            down += [unit for unit in ('caddy', *self.sync_units)
                     if not self.ok(['systemctl', 'start', unit], **QUIET)]
        if self.control_stopped and not self.ok(['systemctl', 'start', *CONTROL_UNITS], **QUIET):
            down.append('vpn2-control')
        down += [name for name in CONTAINERS if self.ok(['docker', 'container', 'inspect', name], **QUIET)
                 and not self.ok(['docker', 'start', name], **QUIET)]
        _warn(f'возвращено из страховочной копии: {" ".join(back) or "ничего не заменялось"}')
        if failed:
            _warn(f'НЕ возвращено: {" ".join(failed)} — бери из {self.presnap_final}')
        _warn(f'не запустились: {" ".join(down)}' if down else 'службы и контейнеры прежнего состояния запущены')
        _warn(f'страховочный снимок: {self.presnap_final}')


def stop_services(rb):
    """Остановка писателей перед заменой; остановленные sync-юниты Caddy запоминаются для старта."""
    _say('останавливаю сервисы…')
    if os.path.isdir(os.path.join(rb.bundle, 'caddy-storage')):
        for unit in SYNC_UNITS:
            if rb.ok(['systemctl', 'is-active', '--quiet', unit]):
                rb.sync_units.append(unit)
                rb.must(['systemctl', 'stop', unit])
        if rb.exists(CERT_SYNC):
            rb.must(['systemctl', 'stop', CERT_SYNC])
        rb.must(['systemctl', 'stop', 'caddy'])
    for unit in STOP_UNITS:
        rb.code(['systemctl', 'stop', unit], stderr=subprocess.DEVNULL)
    for name in CONTAINERS:
        if rb.ok(['docker', 'container', 'inspect', name], **QUIET):
            rb.must(['docker', 'stop', name], stdout=subprocess.DEVNULL)
        else:
            rb.must(['docker', 'info'], stdout=subprocess.DEVNULL)


def carry_revocations(rb):
    """Отзывы, сделанные на узле после архива, переносятся в набор: отозванное не оживает."""
    rb.step = 'перенос отзывов в архив'
    cause = 'перенос отзывов'
    try:
        code = revoke.run(rb.bundle, os.path.join(rb.work, 'issued-after-archive'), live_db=rb.live(DB),
                          live_pki=rb.live('etc/openvpn/server/pki'), live_wg_dir=rb.live('etc/wireguard'),
                          live_ike_conf=rb.live('etc/swanctl/conf.d/anysda-clients.conf'),
                          live_ccd=rb.live('etc/openvpn/server/ccd'), runner=rb.run)
    except RestoreError as exc:
        print(exc.message, file=sys.stderr, flush=True)
        raise StepFailed(exc.code, cause) from None
    if code:
        raise StepFailed(code, cause)


def _db(rb):
    """Новая эпоха control, БД рядом и атомарная замена; WAL/SHM прежней БД уходят в рабочий каталог."""
    rb.touched.append('db')
    if rb.ok(['id', 'vpn2-control'], **QUIET):
        rb.control_stopped = True
        rb.must(['systemctl', 'stop', 'vpn2-control.service', 'vpn2-control.socket'])
        if os.path.isdir(rb.live(CONTROL_DIR)):
            rb.must(['mv', rb.live(CONTROL_DIR), f'{rb.work}/control-before-restore'])
        rb.must(['install', '-d', '-o', 'vpn2-control', '-g', 'vpn2-control', '-m', '0700', rb.live(CONTROL_DIR)])
    db, tmp, before = rb.live(DB), rb.live(f'{DB_DIR}/.db-restore.sqlite'), f'{rb.work}/db-before'
    os.makedirs(rb.live(DB_DIR), exist_ok=True)
    os.makedirs(before, exist_ok=True)
    rb.must(['install', '-m', '600', f'{rb.bundle}/db.sqlite', tmp])
    for suffix in ('wal', 'shm', 'journal'):
        if os.path.exists(f'{db}-{suffix}'):
            rb.must(['mv', f'{db}-{suffix}', before + '/'])
    rb.must(['mv', tmp, db])
    rb.db_swapped = True
    rb.must(['chown', 'root:root', db])


def _etc_anysda(rb):
    rb.touched.append('etc/anysda')
    rb.must(['rm', '-f', f'{rb.bundle}/etc-anysda/source-root'])
    rb.copy_into(f'{rb.bundle}/etc-anysda', rb.live('etc/anysda'))
    for name in ('sso-runtime.json', 'telegram-from-config.json'):
        if not os.path.isfile(f'{rb.bundle}/etc-anysda/{name}'):
            rb.must(['rm', '-f', rb.live(f'etc/anysda/{name}')])


def _wireguard(rb):
    source, wg = f'{rb.bundle}/etc-wireguard', rb.live('etc/wireguard')
    if os.path.isfile(f'{source}/wg0.conf'):
        rb.touched.append('etc/wireguard')
        os.makedirs(wg, exist_ok=True)
        rb.must(['cp', '-a', f'{source}/wg0.conf', f'{wg}/wg0.conf'])
        rb.must(['chmod', '600', f'{wg}/wg0.conf'])
    if os.path.isfile(f'{source}/server.priv'):
        rb.touched.append('etc/wireguard')
        # Режим живого /etc/wireguard не трогаем: 700 только у keys/ (и у нового каталога по umask restore).
        rb.must(['install', '-d', '-m', '700', f'{wg}/keys'])
        for key in ('server.priv', 'server.pub'):
            rb.must(['install', '-m', '600', f'{source}/{key}', f'{wg}/keys/{key}'])
            rb.must(['ln', '-sfn', f'keys/{key}', f'{wg}/{key}'])


def _openvpn(rb):
    rb.touched.append('etc/openvpn')
    rb.copy_into(f'{rb.bundle}/etc-openvpn', rb.live('etc/openvpn'))
    # Устройства, выданные после архива: их ccd в наборе нет, живые файлы убираются.
    issued = Path(rb.work, 'issued-after-archive')
    for device_id in issued.read_text().split() if issued.is_file() else ():
        rb.must(['rm', '-f', rb.live(f'etc/openvpn/server/ccd/dev_{device_id}')])


def _ikev2(rb):
    for name in ('strongswan', 'swanctl', 'strongswan.conf', 'strongswan.d', 'ipsec.conf', 'ipsec.secrets'):
        source = f'{rb.bundle}/etc-{name}'
        if os.path.isdir(source):
            rb.touched.append(f'etc/{name}')
            rb.copy_into(source, rb.live(f'etc/{name}'))
        elif os.path.isfile(source) and '.' in name:
            rb.touched.append(f'etc/{name}')
            rb.must(['cp', '-a', source, rb.live(f'etc/{name}')])


def _caddy(rb):
    if not os.path.isdir(f'{rb.bundle}/caddy-storage'):
        return _say('Caddy storage отсутствует в архиве — оставляю текущее')
    storage = rb.live(CADDY_STORAGE)
    rb.touched += [CADDY_STORAGE, 'etc/anysda']
    rb.must(['install', '-d', '-m', '700', '-o', 'caddy', '-g', 'caddy',
             rb.live('var/lib/caddy/.local'), rb.live('var/lib/caddy/.local/share')])
    rb.must(['rm', '-rf', '--', storage])
    rb.must(['cp', '-a', f'{rb.bundle}/caddy-storage', storage])
    rb.must(['rm', '-rf', '--', f'{storage}/locks'])
    rb.must(['chown', '-R', 'caddy:caddy', storage])
    for kind, mode in (('d', '700'), ('f', '600')):
        rb.must(['find', storage, '-type', kind, '-exec', 'chmod', mode, '{}', '+'])
    rb.must(['rm', '-f', rb.live('etc/anysda/ikev2-cert.sha256')])
    _say('Caddy storage restored (caddy:caddy, dirs=700 files=600)')


def _deploy_state(rb):
    """Состояние деплоя из набора; манифест выпуска остаётся от установленного дерева.

    По манифесту из архива стадии подняли бы образы выпуска, с которого снят бэкап.
    """
    state = rb.live(f'{DB_DIR}/state')
    rb.touched.append(f'{DB_DIR}/state')
    if not os.path.isdir(state):
        rb.must(['install', '-d', '-m', '700', '-o', 'root', '-g', 'root', state])
    manifest, kept = f'{state}/release-manifest.json', f'{state}/.release-manifest.json.kept'
    installed = os.path.isfile(manifest)
    if installed:
        owner = os.stat(manifest)
        shutil.copy2(manifest, kept)
        os.chown(kept, owner.st_uid, owner.st_gid)
    try:
        rb.copy_into(f'{rb.bundle}/deploy-state', state)
    finally:
        if installed:
            os.replace(kept, manifest)


APPLY = (('установка БД', 'db.sqlite', _db), ('восстановление /etc/anysda', 'etc-anysda', _etc_anysda),
         ('восстановление WireGuard', '', _wireguard), ('распаковка PKI OpenVPN', 'etc-openvpn', _openvpn),
         ('восстановление IKEv2', '', _ikev2), ('восстановление хранилища Caddy', '', _caddy),
         ('восстановление состояния деплоя', 'deploy-state', _deploy_state))


def apply_bundle(rb):
    """Замена путей узла из набора; компонента в наборе нет - шаг пропущен."""
    for rb.step, component, step in APPLY:
        if os.path.exists(os.path.join(rb.bundle, component)):
            step(rb)


def _panel_runtime(rb):
    """Рабочие копии настроек для панели vpn2-panel и ссылки на них из /etc/anysda."""
    runtime_dir = rb.live(f'{DB_DIR}/runtime')
    rb.touched += [f'{DB_DIR}/runtime', 'etc/anysda']
    rb.must(['install', '-d', '-o', 'vpn2-panel', '-g', 'vpn2-panel', '-m', '0700', runtime_dir])
    for name in ('sso-runtime.json', 'telegram-runtime.json'):
        runtime, conf = f'{runtime_dir}/{name}', rb.live(f'etc/anysda/{name}')
        if os.path.isfile(conf) and not os.path.islink(conf):
            rb.must(['install', '-o', 'vpn2-panel', '-g', 'vpn2-panel', '-m', '0640', conf, runtime])
        elif not os.path.exists(conf):
            rb.must(['rm', '-f', runtime])
        rb.must(['ln', '-sfn', runtime, conf])
    rb.must(['chown', 'vpn2-panel:vpn2-panel', rb.live(DB_DIR)])
    if os.path.isfile(rb.live(DB)):
        rb.must(['chown', '-h', 'vpn2-panel:vpn2-panel', rb.live(DB)])


def start_services(rb):
    """Старт служб обратно; отказ существующей службы - откат."""
    _say('запускаю сервисы…')
    rb.start_unit('sing-box')
    if os.path.isfile(rb.live('etc/wireguard/wg0.conf')):
        lib = rb.live('usr/local/lib/anysda/anysda-svc.sh')
        if not os.access(lib, os.R_OK):
            rb.code(['systemctl', 'restart', 'wg-quick@wg0'], stderr=subprocess.DEVNULL)
        elif not rb.ok(['bash', '-c', 'source "$1" && wg0_apply', 'anysda-restore', lib]):
            _warn('WARN: wg0 не применился')
    for unit in START_UNITS:
        rb.start_unit(unit)
    if shutil.which('swanctl'):
        rb.code(['swanctl', '--load-all'], **QUIET)
    if os.path.isdir(os.path.join(rb.bundle, 'caddy-storage')):
        rb.must(['systemctl', 'start', 'caddy'])
        if filled(rb.live('etc/anysda/ikev2-domain')) and rb.exists(CERT_SYNC):
            rb.must(['systemctl', 'start', CERT_SYNC])
        if rb.sync_units:
            rb.must(['systemctl', 'start', *rb.sync_units])
    if rb.ok(['id', 'vpn2-panel'], **QUIET):
        _panel_runtime(rb)
    if rb.ok(['id', 'vpn2-control'], **QUIET):
        rb.must(['systemctl', 'start', *CONTROL_UNITS])
    rb.code(['docker', 'start', *CONTAINERS], stderr=subprocess.DEVNULL)


def mark_good(rb):
    """Работающий OpenVPN отмечается last good; отказ - только WARN."""
    tool = rb.live('usr/local/sbin/vpn2-apply')
    if os.access(tool, os.X_OK) and rb.ok(['systemctl', 'is-active', '--quiet', 'openvpn-server@server']) \
            and not rb.ok([tool, 'mark', 'openvpn']):
        _warn('WARN: vpn2-apply mark openvpn не прошёл: last good не совпадает с server.conf')


# После замены: стадии с паролями из архива и проверка узла

def stage_env(source_root, work):
    """Окружение стадий entry из config.yaml клона (инвентарь деплоя) без SSH_PASS; путь к файлу.

    Конфиг не читается - файл не создаётся. ADMIN_PASSWORD снимается: стадии читают
    восстановленный файл, а не засевают новый.
    """
    root = Path(source_root)
    target = Path(work) / 'ru.env'
    found = subprocess.run([sys.executable, str(root / 'infra/ansible/inventory.py'), '--env', 'ru'],
                           capture_output=True, check=False,
                           env={**os.environ, 'PYTHONPATH': str(root / 'infra/vpn2/src'),
                                'VPN2_CONFIG': str(root / 'config.yaml')})
    if found.returncode:
        return target
    own = b''.join(line for line in found.stdout.splitlines(keepends=True) if not line.startswith(b'SSH_PASS='))
    tail = f"unset ADMIN_PASSWORD\nVPN2_DEVELOPMENT={shlex.quote(os.environ.get('VPN2_DEVELOPMENT') or 'false')}\n"
    # /tmp/anysda/release-manifest.json живёт до перезагрузки: деплой оставляет манифест
    # установленного выпуска в состоянии, стадии берут его оттуда.
    manifest = os.path.join(os.environ.get('VPN2_STATE_DIR') or f'/{DB_DIR}/state', 'release-manifest.json')
    if os.path.isfile(manifest):
        tail += f'VPN2_RELEASE_MANIFEST={shlex.quote(manifest)}\n'
    _write_private(target, own + tail.encode())
    return target


def resync(source_root, env_file, work):
    """Стадии 22, 30, 35 с восстановленными паролями; вернуть причину отказа ('' - всё прошло).

    Первая неудача останавливает цепочку; вывод стадии - в work/<стадия>.log, его хвост - в причине.
    """
    for stage, steps in STAGES.items():
        if not filled(env_file):
            fail = f'не прочитан {source_root}/config.yaml: окружение стадий не собрано'
        else:
            _say(f'{stage} с восстановленными паролями…')
            log = Path(work) / f'{stage}.log'
            with open(log, 'wb') as handle:
                code = archive.code(['bash', '-c', RESYNC, 'anysda-restore', str(env_file), str(source_root), *steps],
                                    stdout=handle, stderr=subprocess.STDOUT)
            if code == 0:
                _say(f'{stage} re-synced')
                continue
            tail = log.read_text(errors='replace').splitlines()[-15:]
            fail = '\n'.join([f'{stage} завершился с ошибкой, хвост вывода:', *('    ' + line for line in tail)])
        _warn(f'ERROR: пароли не пересинхронизированы — {fail}')
        return fail
    return ''


def smoke(source_root, env_file, presnap_final):
    """vpn2 verify пакетом установленного дерева: код 2 - предупреждение, иной отказ - RestoreError(7)."""
    package = Path(source_root) / 'infra/vpn2/src'
    if not ((package / 'vpn2/verify/stage.py').is_file() and filled(env_file)):
        return _warn(f'WARN: нет vpn2 verify ({package}) или env стадий — пропуск smoke-test')
    _say('99-verify…')
    code = archive.code(['bash', '-c', VERIFY, 'anysda-restore', str(env_file), str(package), sys.executable])
    if code == 0:
        _say('99-verify PASSED')
    elif code == 2:
        _warn('⚠ 99-verify: не все проверки измерены; причины выше')
    else:
        raise RestoreError(7, f'anysda-restore: 99-verify FAILED — проверь руками; pre-restore снимок: {presnap_final}')


def restore(argv, *, root='/', env_file=env.DEFAULT_ENV_FILE):
    """Восстановить узел из архива; ошибки шагов - RestoreError с кодом выхода и текстом для stderr."""
    try:
        settings = env.settings(env.read_env(env_file), env_file, lazy_s3=True)
    except env.BackupEnvError as exc:
        text = f'anysda-restore: {env_file} не доступен' if exc.code == 2 else str(exc)
        raise RestoreError(exc.code, text) from None
    args = parse_args(argv)
    if not settings.passphrase:
        raise RestoreError(2, NO_PASSPHRASE.format(env_file=env_file))
    name, source = _pick(args, settings)
    if args.dry_run:
        return _say(f'dry-run — восстановил бы {name} ({source}), ничего не изменено')
    if source == 'S3':
        s3_pull(settings, name)
    archive_path = os.path.join(settings.local_dir, name)
    _say(f'source = {archive_path}')
    try:
        # Клон, из которого шёл деплой (отметку пишет стадия 26-backup).
        source_root = Path(root, 'etc/anysda/source-root').read_text(errors='surrogateescape').rstrip('\n')
    except OSError:
        source_root = ''
    source_root = source_root or '/opt/anysda-vpn2'
    live_db = os.path.join(root, DB)
    with archive.workdir(os.path.join(root, DB_DIR), '.restore-tmp.') as work:
        bundle = _open(archive_path, work, settings.passphrase)
        manifest = check_manifest(bundle, name)
        check_compat(manifest, bundle, work, source_root, args.allow_incompatible)
        check_bundle_report(bundle)
        wg_key_material(bundle)
        config_template(source_root, work / 'template' if args.preflight else os.path.join(root, 'tmp/anysda'))
        guards(bundle, live_db, args.force)
        if args.preflight:
            return
        presnap = pre_restore_snapshot(settings, work, root, live_db)
        rb = Rollback(presnap_root=work / 'pre-restore', presnap_final=presnap, work=work, bundle=bundle, root=root)
        with rb:
            stop_services(rb)
            carry_revocations(rb)
            apply_bundle(rb)
            rb.step = 'запуск служб'
            start_services(rb)
        mark_good(rb)
        stages_env = stage_env(source_root, work)
        fail = resync(source_root, stages_env, work)
        smoke(source_root, stages_env, presnap)
    if fail:
        raise RestoreError(8, '\nanysda-restore: FAILED — данные восстановлены, но пароли не пересинхронизированы:\n'
                              f'  {fail}\n'
                              f'  Почини причину и прогони ./deploy.sh <стадия> ru; pre-restore снимок: {presnap}')
    print('\nanysda-restore: SUCCESS\n'
          f'  source:       {archive_path}\n'
          f'  pre-snapshot: {presnap}\n'
          '  Существующие клиенты должны переподключаться без переоформления конфигов.', flush=True)


def main(argv, **kwargs):
    """Код выхода команды; текст RestoreError - в stderr (откат уже отчитался сам)."""
    old_umask = os.umask(0o077)
    try:
        with archive.exit_on_signals():
            restore(argv, **kwargs)
    except RestoreError as exc:
        if exc.code != 10:
            print(exc.message, file=sys.stderr, flush=True)
        return exc.code
    finally:
        os.umask(old_umask)
    return 0
