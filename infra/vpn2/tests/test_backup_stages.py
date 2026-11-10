"""Хвост restore: окружение стадий против блока скрипта, отказы стадий и 99-verify."""
import os
import re
import shutil
import subprocess
from io import StringIO
from pathlib import Path

import pytest

from vpn2.backup import stages
from vpn2.backup.errors import RestoreError

SCRIPT = Path(__file__).resolve().parent / 'fixtures' / 'backup' / 'anysda-restore.sh'
needs_bash = pytest.mark.skipif(shutil.which('bash') is None, reason='нужен bash')


def tree(tmp_path, ru='SSH_PASS=secret value\nHOST=ru\n'):
    src, work = tmp_path / 'src', tmp_path / 'work'
    (src / 'infra/envs').mkdir(parents=True)
    (src / 'infra/scripts').mkdir()
    work.mkdir()
    (src / 'infra/envs/all.env').write_text('AGH_PORT=3000')
    (src / 'infra/envs/ru.env').write_text(ru)
    return src, work


def script(src, name, body):
    path = src / 'infra/scripts' / f'{name}.sh'
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)


def bash_stage_env(src, work, mode):
    block = re.search(r'^ENVS_DIR=.*?^fi$', SCRIPT.read_text(), re.S | re.M).group(0)
    environ = {k: v for k, v in os.environ.items() if k != 'VPN2_DEVELOPMENT'}
    if mode is not None:
        environ['VPN2_DEVELOPMENT'] = mode
    subprocess.run(['bash', '-c', f'SOURCE_ROOT={src}\nWORK={work}\n{block}\n'], env=environ, check=True, timeout=30)
    return (work / 'ru.env').read_bytes()


@needs_bash
@pytest.mark.parametrize('mode', [None, '', 'true', 'false'])
@pytest.mark.parametrize('ru', ['SSH_PASS=secret value\nHOST=ru\n', 'HOST=ru\nX_SSH_PASS=1', ''])
def test_stage_env_matches_bash(tmp_path, mode, ru):
    src, work = tree(tmp_path, ru)
    old = bash_stage_env(src, work, mode)
    (work / 'ru.env').unlink()
    target = stages.stage_env(src, work, environ={} if mode is None else {'VPN2_DEVELOPMENT': mode})
    assert target.read_bytes() == old
    assert oct(target.stat().st_mode & 0o777) == '0o600'
    assert b'SSH_PASS=secret' not in old and b'unset ADMIN_PASSWORD\n' in old


def test_stage_env_absent_without_envs(tmp_path):
    src, work = tree(tmp_path)
    (src / 'infra/envs/all.env').unlink()
    assert not stages.stage_env(src, work).exists()


def test_resync_reports_missing_script_and_env(tmp_path):
    src, work = tree(tmp_path)
    out, err = StringIO(), StringIO()
    env_file = stages.stage_env(src, work, environ={})
    assert stages.resync(src, env_file, work, out=out, err=err) == f'нет {src}/infra/scripts/22-adguard.sh'
    assert err.getvalue() == ('anysda-restore: ERROR: пароли не пересинхронизированы — '
                              f'нет {src}/infra/scripts/22-adguard.sh\n')
    script(src, '22-adguard', 'exit 0\n')
    env_file.unlink()
    envs = f'{src}/infra/envs'
    assert stages.resync(src, env_file, work, out=out, err=StringIO()) == (
        f'нет {envs}/all.env или {envs}/ru.env (их пишет ./deploy.sh из config.yaml)')
    assert out.getvalue() == ''


def test_resync_keeps_tail_of_failed_stage(tmp_path):
    src, work = tree(tmp_path)
    script(src, '22-adguard', 'echo ok\n')
    script(src, '30-frontend', 'seq 1 20\necho беда >&2\nexit 3\n')
    script(src, '35-telegram', 'touch "$(dirname "$1")/ran-35"\n')
    out, err = StringIO(), StringIO()
    fail = stages.resync(src, stages.stage_env(src, work, environ={}), work, out=out, err=err)
    lines = fail.split('\n')
    assert lines[0] == '30-frontend завершился с ошибкой, хвост вывода:'
    assert lines[1:] == [f'    {n}' for n in range(7, 21)] + ['    беда']
    assert out.getvalue() == ('anysda-restore: 22-adguard с восстановленными паролями…\n'
                              'anysda-restore: 22-adguard re-synced\n'
                              'anysda-restore: 30-frontend с восстановленными паролями…\n')
    assert (work / '22-adguard.log').read_text() == 'ok\n' and not (work / 'ran-35').exists()


def test_smoke_skips_without_verify_and_fails_on_other_codes(tmp_path):
    src, work = tree(tmp_path)
    env_file = stages.stage_env(src, work, environ={})
    out, err = StringIO(), StringIO()
    stages.smoke(src, env_file, 'снимок', out=out, err=err)
    assert err.getvalue() == (f'anysda-restore: WARN: нет 99-verify ({src}/infra/scripts/99-verify.sh) '
                              'или env стадий — пропуск smoke-test\n')
    script(src, '99-verify', 'exit 3\n')
    with pytest.raises(RestoreError) as caught:
        stages.smoke(src, env_file, 'снимок', out=out, err=err)
    assert caught.value.code == 7 and caught.value.message.endswith('pre-restore снимок: снимок')
    assert out.getvalue() == 'anysda-restore: 99-verify…\n'


def test_finish_reports_unsynced_passwords():
    with pytest.raises(RestoreError) as caught:
        stages.finish('причина', 'архив', 'снимок', out=StringIO())
    assert caught.value.code == 8
    assert caught.value.message == (
        '\nanysda-restore: FAILED — данные восстановлены, но пароли не пересинхронизированы:\n'
        '  причина\n  Почини причину и прогони ./deploy.sh <стадия> ru; pre-restore снимок: снимок')
