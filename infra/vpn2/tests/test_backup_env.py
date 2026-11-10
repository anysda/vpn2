import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from vpn2.backup.env import BackupEnvError, Settings, bundle_name, read_env, settings, timestamp

HAS_BASH = shutil.which('bash') is not None
NEEDS_BASH = pytest.mark.skipif(not HAS_BASH, reason='нужен bash')
SCRIPT = Path(__file__).resolve().parents[2] / 'lib' / 'anysda-backup.sh'
VALUES = {
    'BACKUP_BACKEND': 'local',
    'WITH_SPACE': 'a b  c',
    'WITH_QUOTES': 'it\'s "q"',
    'WITH_DOLLAR': '$HOME `id` $(x)',
    'WITH_NEWLINE': 'line1\nline2',
    'CYRILLIC': 'пароль и ключ',
    'EMPTY': '',
}


def write_env(path, values):
    path.write_text(''.join(f'{k}={_q(v)}\n' for k, v in values.items()))
    return path


def _q(value):
    return subprocess.run(['bash', '-c', 'printf %q "$1"', '_', value],
                          capture_output=True, text=True, check=True).stdout


@NEEDS_BASH
def test_read_env_matches_bash_source(tmp_path):
    # прямая сверка с bash: удалить вместе с bash-телом
    path = write_env(tmp_path / 'backup.env', VALUES)
    got = read_env(path)
    for key, value in VALUES.items():
        expected = subprocess.run(
            ['bash', '-c', 'source "$1"; printf %s "${!2}"', '_', str(path), key],
            capture_output=True, text=True, check=True).stdout
        assert expected == value
        assert got[key] == expected
    assert set(got) == set(VALUES)


@NEEDS_BASH
def test_read_env_skips_base_environment_and_repeats(tmp_path, monkeypatch):
    monkeypatch.setenv('LEAK_FROM_PARENT', 'x')
    path = write_env(tmp_path / 'e', {'BACKUP_BACKEND': 's3'})
    assert read_env(path) == {'BACKUP_BACKEND': 's3'}
    assert read_env(path) == {'BACKUP_BACKEND': 's3'}


def test_read_env_missing_file_is_error_like_script(tmp_path):
    with pytest.raises(BackupEnvError) as info:
        read_env(tmp_path / 'none.env')
    assert 'отсутствует или unreadable' in str(info.value)
    assert info.value.code == 2


def test_read_env_unreadable_directory(tmp_path):
    with pytest.raises(BackupEnvError):
        read_env(tmp_path)


def test_read_env_bash_failure_and_missing_bash(tmp_path):
    path = tmp_path / 'e'
    path.write_text('A=1\n')
    failed = subprocess.CompletedProcess([], 3, b'', b'')
    with pytest.raises(BackupEnvError, match='код 3'):
        read_env(path, run=lambda *_a, **_k: failed)

    def boom(*_a, **_k):
        raise FileNotFoundError('bash')
    with pytest.raises(FileNotFoundError):
        read_env(path, run=boom)


def test_read_env_fake_run_diffs_base(tmp_path):
    path = tmp_path / 'e'
    path.write_text('')
    outs = iter([b'PWD=/x\0A=1\0_=/y\0', b'PWD=/x\0'])
    run = lambda *_a, **_k: subprocess.CompletedProcess([], 0, next(outs), b'')  # noqa: E731
    assert read_env(path, run=run) == {'A': '1'}


def test_settings_defaults_like_script_lines_255_257():
    assert SCRIPT.read_text().count(':=/var/backups/anysda-vpn2}') == 1
    s = settings({'BACKUP_BACKEND': 'local'})
    assert (s.backend, s.local_dir, s.retention) == ('local', '/var/backups/anysda-vpn2', '10')
    assert settings({'BACKUP_BACKEND': 'local', 'BACKUP_LOCAL_DIR': '', 'BACKUP_RETENTION': ''}).retention == '10'


def test_settings_required_backend_text():
    with pytest.raises(BackupEnvError, match='BACKUP_BACKEND не задан в /etc/anysda/backup.env'):
        settings({})
    with pytest.raises(BackupEnvError, match='BACKUP_BACKEND'):
        settings({'BACKUP_BACKEND': ''}, '/x')


def test_settings_s3_required_and_region():
    base = {'BACKUP_BACKEND': 's3'}
    with pytest.raises(BackupEnvError, match='BACKUP_S3_ENDPOINT не задан'):
        settings(base)
    base['BACKUP_S3_ENDPOINT'] = 'https://e'
    with pytest.raises(BackupEnvError, match='BACKUP_S3_BUCKET не задан'):
        settings(base)
    base['BACKUP_S3_BUCKET'] = 'b'
    with pytest.raises(BackupEnvError, match=r'ANYSDA_S3_ACCESS_KEY не задан \(из /f\)'):
        settings(base, '/f')
    base['ANYSDA_S3_ACCESS_KEY'] = 'k'
    with pytest.raises(BackupEnvError, match='ANYSDA_S3_SECRET_KEY'):
        settings(base)
    base['ANYSDA_S3_SECRET_KEY'] = 's'
    s = settings(base)
    assert s.s3_region == 'us-east-1' and s.s3_bucket == 'b'
    assert (s.s3_endpoint, s.s3_access_key, s.s3_secret_key) == ('https://e', 'k', 's')
    assert settings({**base, 'ANYSDA_BACKUP_PASSPHRASE': 'pp'}).passphrase == 'pp'
    assert settings({**base, 'BACKUP_S3_REGION': 'ru-1'}).s3_region == 'ru-1'


def test_secrets_not_in_repr():
    s = Settings(backend='s3', passphrase='hunter-two', s3_access_key='AK1', s3_secret_key='SK2')
    text = repr(s)
    assert 'hunter-two' not in text and 'AK1' not in text and 'SK2' not in text


def test_settings_is_frozen():
    with pytest.raises(AttributeError):
        settings({'BACKUP_BACKEND': 'x'}).backend = 'y'


@NEEDS_BASH
def test_timestamp_matches_date_u():
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    expected = subprocess.run(['date', '-u', '-d', '@%d' % moment.timestamp(), '+%Y-%m-%dT%H%M%SZ'],
                              capture_output=True, text=True, check=True).stdout.strip()
    assert timestamp(moment) == expected == '2026-01-02T030405Z'


def test_timestamp_converts_zone_and_defaults():
    local = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=3)))
    assert timestamp(local) == '2026-01-02T000405Z'
    assert timestamp().endswith('Z') and len(timestamp()) == 18


def test_bundle_name():
    assert bundle_name('2026-01-02T030405Z') == 'anysda-vpn2-2026-01-02T030405Z'
