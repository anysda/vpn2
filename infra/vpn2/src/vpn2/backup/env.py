"""Настройки бэкапа из /etc/anysda/backup.env: чтение, умолчания, метка и имя набора."""
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime

DEFAULT_ENV_FILE = '/etc/anysda/backup.env'
_SCRIPT = 'set -a; source "$1"; env -0'
_BASE_SCRIPT = 'set -a; env -0'


class BackupEnvError(Exception):
    """Настройки бэкапа не прочитаны или неполны; text - как в bash-скрипте."""

    def __init__(self, text, code=2):
        super().__init__(text)
        self.code = code


def _parse(raw):
    result = {}
    for item in raw.split(b'\0'):
        if not item:
            continue
        key, _, value = item.partition(b'=')
        result[os.fsdecode(key)] = os.fsdecode(value)
    return result


def _bash(script, path, run):
    args = ['env', '-i', 'bash', '-c', script, '_']
    if path is not None:
        args.append(str(path))
    done = run(args, capture_output=True, check=False)
    if done.returncode != 0:
        raise BackupEnvError(f'anysda-backup: не удалось прочитать {path}: bash код {done.returncode}', 1)
    return _parse(done.stdout)


def read_env(path=DEFAULT_ENV_FILE, *, run=subprocess.run):
    """Переменные, заданные файлом, как их видит bash после source (с set -a).

    Файл пишется printf %q, поэтому разбор отдан bash: окружение после source
    минус окружение того же bash без файла. Нет файла или он нечитаем - ошибка
    с текстом скрипта (код 2).
    """
    if not os.access(path, os.R_OK) or os.path.isdir(path):
        raise BackupEnvError(f'anysda-backup: {path} отсутствует или unreadable')
    after = _bash(_SCRIPT, path, run)
    base = _bash(_BASE_SCRIPT, None, run)
    return {k: v for k, v in after.items() if k != '_' and base.get(k) != v}


@dataclass(frozen=True)
class Settings:
    """Переменные, которые читает anysda-backup.sh (умолчания - как в строках 255-257)."""

    backend: str
    local_dir: str = '/var/backups/anysda-vpn2'
    retention: str = '10'
    passphrase: str = field(default='', repr=False)
    s3_endpoint: str = ''
    s3_bucket: str = ''
    s3_region: str = 'us-east-1'
    s3_access_key: str = field(default='', repr=False)
    s3_secret_key: str = field(default='', repr=False)


# Порядок - как их раскрывал скрипт: бакет в аргументе, затем ключи и адрес в вызове aws.
S3_NAMES = {'s3_bucket': 'BACKUP_S3_BUCKET', 's3_access_key': 'ANYSDA_S3_ACCESS_KEY',
            's3_secret_key': 'ANYSDA_S3_SECRET_KEY', 's3_endpoint': 'BACKUP_S3_ENDPOINT'}


def _required(env, name, text):
    if not env.get(name):
        raise BackupEnvError(f'{name}: {text}', 1)
    return env[name]


def settings(env, env_file=DEFAULT_ENV_FILE, *, lazy_s3=False):
    """Settings из словаря read_env; пустое значение - как отсутствующее (семантика :- и :?).

    lazy_s3 - не требовать переменные S3 сразу: restore спрашивает их только при обращении к облаку.
    """
    backend = _required(env, 'BACKUP_BACKEND', f'BACKUP_BACKEND не задан в {env_file}')
    values = {
        'backend': backend,
        'local_dir': env.get('BACKUP_LOCAL_DIR') or Settings.local_dir,
        'retention': env.get('BACKUP_RETENTION') or Settings.retention,
        'passphrase': env.get('ANYSDA_BACKUP_PASSPHRASE', ''),
    }
    if backend == 's3' and lazy_s3:
        for key, name in S3_NAMES.items():
            values[key] = env.get(name, '')
        values['s3_region'] = env.get('BACKUP_S3_REGION') or Settings.s3_region
    elif backend == 's3':
        values['s3_endpoint'] = _required(env, 'BACKUP_S3_ENDPOINT', 'BACKUP_S3_ENDPOINT не задан')
        values['s3_bucket'] = _required(env, 'BACKUP_S3_BUCKET', 'BACKUP_S3_BUCKET не задан')
        values['s3_access_key'] = _required(
            env, 'ANYSDA_S3_ACCESS_KEY', f'ANYSDA_S3_ACCESS_KEY не задан (из {env_file})')
        values['s3_secret_key'] = _required(
            env, 'ANYSDA_S3_SECRET_KEY', f'ANYSDA_S3_SECRET_KEY не задан (из {env_file})')
        values['s3_region'] = env.get('BACKUP_S3_REGION') or Settings.s3_region
    return Settings(**values)


def timestamp(now=None):
    """Метка как `date -u +%Y-%m-%dT%H%M%SZ`; now - datetime для подмены."""
    now = now or datetime.now(UTC)
    return now.astimezone(UTC).strftime('%Y-%m-%dT%H%M%SZ')


def bundle_name(ts):
    """Имя набора: anysda-vpn2-<метка>."""
    return f'anysda-vpn2-{ts}'
