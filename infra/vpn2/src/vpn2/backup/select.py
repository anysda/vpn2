"""Выбор архива для restore: разбор аргументов, метка и возраст, latest среди локальных и S3."""
import calendar
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

from vpn2.backup.env import S3_NAMES
from vpn2.backup.errors import RestoreError

_NAME = re.compile(r'anysda-vpn2-([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z)\.tar\.gz\.age')
_FLAGS = {'--force': 'force', '-f': 'force', '--dry-run': 'dry_run',
          '--allow-incompatible': 'allow_incompatible'}


@dataclass(frozen=True)
class Args:
    """Аргументы restore: имя архива (или latest) и флаги."""

    name: str = 'latest'
    force: bool = False
    dry_run: bool = False
    allow_incompatible: bool = False


def parse_args(argv):
    """Разбор как в скрипте: имя необязательно и только первым, дальше флаги в любом порядке."""
    argv = list(argv)
    name = 'latest'
    if argv and argv[0] and not argv[0].startswith('-'):
        name = argv.pop(0)
    flags = {}
    for arg in argv:
        if arg not in _FLAGS:
            raise RestoreError(2, f'anysda-restore: неизвестный аргумент: {arg}')
        flags[_FLAGS[arg]] = True
    return Args(name=name, **flags)


def archive_ts(name):
    """Метка создания из имени архива или None; mtime не годится - меняется при копировании."""
    match = _NAME.fullmatch(name)
    return match.group(1) if match else None


def ts_epoch(ts):
    """Метка вида 2026-05-22T020000Z в секунды эпохи (UTC)."""
    return calendar.timegm((int(ts[0:4]), int(ts[5:7]), int(ts[8:10]),
                            int(ts[11:13]), int(ts[13:15]), int(ts[15:17])))


def age_text(seconds):
    """Возраст словами, как в скрипте: 'N д M ч', 'N ч M мин' или 'N мин' (целое деление к нулю)."""
    def div(a, b):
        return int(a / b) if a < 0 else a // b
    if seconds >= 86400:
        return f'{seconds // 86400} д {seconds % 86400 // 3600} ч'
    if seconds >= 3600:
        return f'{seconds // 3600} ч {seconds % 3600 // 60} мин'
    return f'{div(seconds, 60)} мин'


def _s3_env(settings, environ):
    env = dict(os.environ if environ is None else environ)
    env['AWS_DEFAULT_REGION'] = settings.s3_region
    env['AWS_ACCESS_KEY_ID'] = settings.s3_access_key
    env['AWS_SECRET_ACCESS_KEY'] = settings.s3_secret_key
    return env


def _s3_unset(settings):
    """Сообщение bash о первой пустой переменной S3 (`${VAR:?}` скрипта) или None."""
    for key, name in S3_NAMES.items():
        if not getattr(settings, key):
            return f'{name}: parameter null or not set'
    return None


def _aws(settings, *args):
    return ['aws', '--endpoint-url', settings.s3_endpoint, 's3', *args]


def _newest(names):
    best = None
    for name in names:
        ts = archive_ts(name)
        if ts is not None and (best is None or ts > archive_ts(best)):
            best = name
    return best


def _local_names(local_dir):
    try:
        with os.scandir(local_dir) as entries:
            return [e.name for e in entries if e.is_file(follow_symlinks=False)]
    except OSError:
        return []


def _s3_names(text):
    return [line.strip(' \t').rpartition(' ')[2] for line in text.split('\n')]


def resolve_latest(settings, *, run=subprocess.run, now=None, err=None, environ=None):
    """Самый свежий по метке архив среди локальных и S3: (имя, 'local' | 'S3').

    Недоступный S3 - предупреждение и выбор среди локальных; пусто везде - RestoreError(3).
    Сообщения идут в err (stderr), now - секунды эпохи для подмены.
    """
    err = sys.stderr if err is None else err
    best_local = _newest(_local_names(settings.local_dir))
    best_s3 = None
    if settings.backend == 's3':
        text, code = _s3_unset(settings), 1
        try:
            if text is None:
                done = run(_aws(settings, 'ls', f's3://{settings.s3_bucket}/'), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, env=_s3_env(settings, environ), check=False)
                text, code = done.stdout.decode('utf-8', 'surrogateescape').rstrip('\n'), done.returncode
        except OSError:
            text, code = 'aws: command not found', 127
        if code == 0:
            best_s3 = _newest(_s3_names(text))
        else:
            err.write('anysda-restore: WARN: S3 недоступен — выбираю только среди локальных архивов, '
                      'в облаке может лежать более свежий:\n')
            for line in (text + '\n').split('\n')[:-1][-3:]:
                err.write(f'  {line}\n')
    if best_local is None and best_s3 is None:
        raise RestoreError(3, f"anysda-restore: ни одного backup'а ни в {settings.local_dir}, ни в S3")
    pick, source, other, other_from = best_local, 'local', None, ''
    if best_s3 is not None and (best_local is None or archive_ts(best_s3) > archive_ts(best_local)):
        pick, source, other, other_from = best_s3, 'S3', best_local, 'local'
    elif best_s3 is not None and best_s3 != best_local:
        other, other_from = best_s3, 'S3'
    pick_epoch = ts_epoch(archive_ts(pick))
    now = int(time.time()) if now is None else now
    err.write(f'anysda-restore: latest = {pick} ({source}), создан {archive_ts(pick)}, '
              f'{age_text(now - pick_epoch)} назад\n')
    if other:
        gap = age_text(pick_epoch - ts_epoch(archive_ts(other)))
        err.write(f'anysda-restore:   самый свежий {other_from}: {other} — старше выбранного на {gap}\n')
    return pick, source


def s3_pull(settings, name, dest, *, run=subprocess.run, environ=None):
    """Скачать архив из S3 в каталог dest с правами 0600; False при любом отказе (aws, chmod)."""
    os.makedirs(dest, mode=0o700, exist_ok=True)
    target = os.path.join(dest, name)
    unset = _s3_unset(settings)
    if unset:
        raise RestoreError(1, f'anysda-restore: {unset}')
    try:
        done = run(_aws(settings, 'cp', f's3://{settings.s3_bucket}/{name}', target, '--only-show-errors'),
                   env=_s3_env(settings, environ), check=False)
        if done.returncode != 0:
            return False
        os.chmod(target, 0o600)  # aws s3 cp следует umask - выставляем явно
    except OSError:
        return False
    return True


def dry_run_text(name, source):
    """Строка dry-run: какой архив и откуда был бы взят."""
    return f'anysda-restore: dry-run — восстановил бы {name} ({source}), ничего не изменено'
