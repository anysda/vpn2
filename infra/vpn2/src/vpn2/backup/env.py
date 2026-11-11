"""Настройки бэкапа из /etc/anysda/backup.env и вызов aws с ними."""
import os
import subprocess
from dataclasses import dataclass, field

from vpn2.backup import archive

DEFAULT_ENV_FILE = '/etc/anysda/backup.env'
S3_NAMES = {'s3_bucket': 'BACKUP_S3_BUCKET', 's3_access_key': 'ANYSDA_S3_ACCESS_KEY',
            's3_secret_key': 'ANYSDA_S3_SECRET_KEY', 's3_endpoint': 'BACKUP_S3_ENDPOINT'}


class BackupEnvError(Exception):
    """Настройки бэкапа не прочитаны (код 2) или неполны (код 1)."""

    def __init__(self, text, code=2):
        super().__init__(text)
        self.code = code


def _bash_env(script, *args):
    done = subprocess.run(['env', '-i', 'bash', '-c', script, '_', *args], capture_output=True, check=False)
    if done.returncode != 0:
        raise BackupEnvError(f'anysda-backup: не удалось прочитать {" ".join(args)}: bash код {done.returncode}', 1)
    pairs = (item.partition(b'=') for item in done.stdout.split(b'\0') if item)
    return {os.fsdecode(key): os.fsdecode(value) for key, _, value in pairs}


def read_env(path=DEFAULT_ENV_FILE):
    """Переменные файла, как их видит bash после source: файл пишется printf %q, разбор отдан bash."""
    if not os.access(path, os.R_OK) or os.path.isdir(path):
        raise BackupEnvError(f'anysda-backup: {path} отсутствует или unreadable')
    base = _bash_env('set -a; env -0')
    after = _bash_env('set -a; source "$1"; env -0', str(path))
    return {key: value for key, value in after.items() if key != '_' and base.get(key) != value}


@dataclass(frozen=True)
class Settings:
    backend: str
    local_dir: str = '/var/backups/anysda-vpn2'
    retention: str = '10'
    passphrase: str = field(default='', repr=False)
    s3_endpoint: str = ''
    s3_bucket: str = ''
    s3_region: str = 'us-east-1'
    s3_access_key: str = field(default='', repr=False)
    s3_secret_key: str = field(default='', repr=False)


def settings(env, env_file=DEFAULT_ENV_FILE, *, lazy_s3=False):
    """Settings из словаря read_env; пустое значение - как отсутствующее.

    lazy_s3 - не требовать переменные S3 сразу: restore спрашивает их только при обращении к облаку.
    """
    if not env.get('BACKUP_BACKEND'):
        raise BackupEnvError(f'BACKUP_BACKEND: BACKUP_BACKEND не задан в {env_file}', 1)
    values = Settings(backend=env['BACKUP_BACKEND'],
                      local_dir=env.get('BACKUP_LOCAL_DIR') or Settings.local_dir,
                      retention=env.get('BACKUP_RETENTION') or Settings.retention,
                      passphrase=env.get('ANYSDA_BACKUP_PASSPHRASE', ''),
                      s3_region=env.get('BACKUP_S3_REGION') or Settings.s3_region,
                      **{key: env.get(name, '') for key, name in S3_NAMES.items()})
    if values.backend == 's3' and not lazy_s3 and (unset := _s3_unset(values)):
        raise BackupEnvError(f'{unset}: {unset} не задан (из {env_file})', 1)
    return values


def _s3_unset(values):
    return next((name for key, name in S3_NAMES.items() if not getattr(values, key)), None)


def aws(values, *args, capture=False):
    """`aws s3 ...` с ключами из настроек: (код, stdout при capture, причина отказа); stderr aws виден оператору."""
    if unset := _s3_unset(values):
        return 1, f'{unset}: parameter null or not set', ''
    environ = {**os.environ, 'AWS_DEFAULT_REGION': values.s3_region,
               'AWS_ACCESS_KEY_ID': values.s3_access_key, 'AWS_SECRET_ACCESS_KEY': values.s3_secret_key}
    return archive.call(['aws', '--endpoint-url', values.s3_endpoint, 's3', *args], env=environ,
                        stdout=subprocess.PIPE if capture else None)
