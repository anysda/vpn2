"""Проверки restore до любых изменений: sha компонентов по манифесту, формат и схема БД."""
import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

from vpn2.backup import bundle
from vpn2.backup.errors import RestoreError

DEFAULT_SOURCE_ROOT = '/opt/anysda-vpn2'
CONTAINER_MIGRATIONS = 'anysda-vpn2:/app/server/database/migrations'
JOURNAL = 'meta/_journal.json'


def check_manifest(bundle_dir, *, archive_name=None, out=None, err=None):
    """Сверить компоненты набора с manifest.json; вернуть манифест.

    Нет манифеста, расхождение, отсутствующий компонент - RestoreError(5) с текстом скрипта.
    archive_name (если дан) сверяется с manifest.timestamp: несовпадение - предупреждение в err.
    Лишние файлы вне манифеста не ошибка. Успех - строка 'manifest verified: ...' в out.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    path = os.path.join(bundle_dir, bundle.MANIFEST)
    if not os.access(path, os.R_OK):
        raise RestoreError(5, 'anysda-restore: manifest.json отсутствует — corrupted архив')
    try:
        with open(path) as f:
            manifest = json.load(f)
        components = manifest.get('components', {})
        total = len(manifest['components'])
    except (ValueError, KeyError, AttributeError, OSError) as exc:
        raise RestoreError(5, f'anysda-restore: manifest.json не разобран: {type(exc).__name__}: {exc}') from exc
    stamp = manifest.get('timestamp')
    if archive_name is not None and archive_name != f'anysda-vpn2-{stamp}.tar.gz.age':
        err.write(f'anysda-restore: WARN: имя архива не совпадает с manifest.timestamp={stamp}\n')
    fail = []
    for name, expected in components.items():
        full = os.path.join(bundle_dir, name)
        if not os.path.exists(full):
            fail.append(f'  {name}: missing')
            continue
        actual = bundle.sha_dir(full) if os.path.isdir(full) else bundle.sha_file(full)
        if actual != expected:
            fail.append(f'  {name}: expected {expected}, got {actual}')
    if fail:
        raise RestoreError(5, '\n'.join(['manifest verification FAILED:', *fail]))
    out.write(f'manifest verified: {total} components,'
              f' archive={stamp} app_v={manifest.get("version")} git={manifest.get("git_commit")}\n')
    return manifest


def _incompatible(text, allow, err):
    if allow:
        err.write(f'anysda-restore: WARN: {text} — продолжаю по --allow-incompatible\n')
        return
    raise RestoreError(9, f'anysda-restore: ABORT — {text}; данные не изменены (обход: --allow-incompatible)')


def _find_journal(work_dir, source_root, run):
    """Каталог миграций: сначала из контейнера панели (docker cp), запасной - клон исходников."""
    copied = os.path.join(work_dir, 'migrations')
    try:
        done = run(['docker', 'cp', CONTAINER_MIGRATIONS, copied],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if done.returncode == 0 and os.access(os.path.join(copied, JOURNAL), os.R_OK):
            return copied
    except OSError:
        pass
    source = os.path.join(source_root, 'app/server/database/migrations')
    return source if os.access(os.path.join(source, JOURNAL), os.R_OK) else None


def _schema(database, folder):
    """Сообщение о схеме БД архива либо SystemExit-текст расхождения (как heredoc скрипта)."""
    with open(os.path.join(folder, JOURNAL)) as f:
        entries = json.load(f)['entries']
    journal = []
    for e in entries:
        with open(os.path.join(folder, e['tag'] + '.sql'), 'rb') as sql:
            journal.append((int(e['when']), hashlib.sha256(sql.read()).hexdigest()))
    with contextlib.closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if '__drizzle_migrations' not in tables:
            if tables:
                raise ValueError('в базе архива нет истории миграций (__drizzle_migrations)')
            history = []
        else:
            history = [(int(when), digest) for when, digest in
                       db.execute('SELECT created_at, hash FROM __drizzle_migrations ORDER BY created_at')]
    tags = [e['tag'] for e in entries]
    if len(history) > len(journal) and history[:len(journal)] == journal:
        raise ValueError(f'схема БД архива новее текущей версии: {len(history)} миграций против {len(journal)}')
    for index, row in enumerate(history):
        if index >= len(journal) or row != journal[index]:
            name = tags[index] if index < len(journal) else f'#{index + 1}'
            raise ValueError(f'история миграций архива расходится с текущей версией на {name}')
    behind = len(journal) - len(history)
    return (f'схема БД архива: {len(history)} из {len(journal)} миграций текущей версии'
            + (f', {behind} догонит стадия 30' if behind else ''))


def check_compat(manifest, bundle_dir, work_dir, *, source_root=DEFAULT_SOURCE_ROOT,
                 run=subprocess.run, allow=False, out=None, err=None):
    """Формат архива (spec_version) и схема БД против журнала drizzle текущей версии.

    Несовместимость - RestoreError(9); при allow - только предупреждение в err и работа дальше.
    Схема сверяется, если в наборе есть db.sqlite; результат сверки - строка в out.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    spec = manifest.get('spec_version', 1)
    if str(spec) != '1':
        _incompatible(f'формат архива spec_version={spec}, этот restore понимает только 1', allow, err)
    database = os.path.join(bundle_dir, 'db.sqlite')
    if not os.path.exists(database):
        return
    folder = _find_journal(work_dir, source_root, run)
    if folder is None:
        _incompatible('не найден журнал миграций текущей версии '
                      f'(ни в контейнере anysda-vpn2, ни в {source_root})', allow, err)
        return
    try:
        result = _schema(database, folder)
    except Exception as exc:  # у скрипта любой отказ heredoc попадает в текст несовместимости
        text = str(exc) if isinstance(exc, ValueError) else f'{type(exc).__name__}: {exc}'
        _incompatible(text, allow, err)
        return
    out.write(f'anysda-restore: {result}\n')
