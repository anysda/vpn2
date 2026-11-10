"""Хвост restore после замены данных: стадии с паролями из архива, 99-verify, итог."""
import os
import shlex
import subprocess
import sys
from pathlib import Path

from vpn2.backup.errors import RestoreError

STAGES = ('22-adguard', '30-frontend', '35-telegram')
TAIL_LINES = 15
STATE_DIR = '/var/lib/anysda-vpn2/state'


def _say(text, stream):
    print(text, file=stream, flush=True)


def stage_env(source_root, work, *, environ=None):
    """Окружение стадий как у deploy.sh: all.env, пустая строка, ru.env без SSH_PASS; путь к файлу.

    Нет all.env или ru.env - файл не создаётся (стадии и 99-verify это видят по пустоте).
    ADMIN_PASSWORD снимается: стадии читают восстановленный файл, а не засевают новый.
    """
    environ = os.environ if environ is None else environ
    envs = Path(source_root) / 'infra/envs'
    target = Path(work) / 'ru.env'
    if not (os.access(envs / 'all.env', os.R_OK) and os.access(envs / 'ru.env', os.R_OK)):
        return target
    common = (envs / 'all.env').read_bytes()
    own = [line for line in (envs / 'ru.env').read_bytes().splitlines(keepends=True)
           if not line.startswith(b'SSH_PASS=')]
    development = environ.get('VPN2_DEVELOPMENT') or 'false'
    tail = f'unset ADMIN_PASSWORD\nVPN2_DEVELOPMENT={shlex.quote(development)}\n'.encode()
    # /tmp/anysda/release-manifest.json живёт до перезагрузки: деплой оставляет манифест
    # установленного выпуска в состоянии, стадии берут его оттуда.
    manifest = os.path.join(environ.get('VPN2_STATE_DIR') or STATE_DIR, 'release-manifest.json')
    if os.path.isfile(manifest):
        tail += f'VPN2_RELEASE_MANIFEST={shlex.quote(manifest)}\n'.encode()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(common + b'\n' + b''.join(own))
        if own and not own[-1].endswith(b'\n'):
            handle.write(b'\n')
        handle.write(tail)
    os.chmod(target, 0o600)
    return target


def _filled(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _log_tail(path):
    lines = Path(path).read_bytes().decode(errors='replace').splitlines()[-TAIL_LINES:]
    return '\n'.join('    ' + line for line in lines).rstrip('\n')


def resync(source_root, env_file, work, *, run=subprocess.run, out=None, err=None):
    """Стадии 22, 30, 35 с восстановленными паролями; вернуть причину отказа ('' - всё прошло).

    Первая неудача останавливает цепочку: строка ERROR в err, вывод стадии - в work/<стадия>.log.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    envs = Path(source_root) / 'infra/envs'
    for stage in STAGES:
        script = Path(source_root) / 'infra/scripts' / f'{stage}.sh'
        if not os.access(script, os.X_OK):
            fail = f'нет {script}'
        elif not _filled(env_file):
            fail = f'нет {envs}/all.env или {envs}/ru.env (их пишет ./deploy.sh из config.yaml)'
        else:
            _say(f'anysda-restore: {stage} с восстановленными паролями…', out)
            log = Path(work) / f'{stage}.log'
            with open(log, 'wb') as handle:
                try:
                    code = run([str(script), str(env_file)], stdout=handle, stderr=subprocess.STDOUT,
                               check=False).returncode
                except OSError as exc:
                    handle.write(f'{script}: {exc.strerror}\n'.encode())
                    code = 126
            if code == 0:
                _say(f'anysda-restore: {stage} re-synced', out)
                continue
            fail = f'{stage} завершился с ошибкой, хвост вывода:\n{_log_tail(log)}'
        _say(f'anysda-restore: ERROR: пароли не пересинхронизированы — {fail}', err)
        return fail
    return ''


def smoke(source_root, env_file, presnap_final, *, run=subprocess.run, out=None, err=None):
    """99-verify установленного дерева: код 2 - предупреждение, иной отказ - RestoreError(7)."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    verify = Path(source_root) / 'infra/scripts/99-verify.sh'
    if not (os.access(verify, os.X_OK) and _filled(env_file)):
        _say(f'anysda-restore: WARN: нет 99-verify ({verify}) или env стадий — пропуск smoke-test', err)
        return
    _say('anysda-restore: 99-verify…', out)
    try:
        code = run([str(verify), str(env_file)], check=False).returncode
    except OSError:
        code = 126
    if code == 0:
        _say('anysda-restore: 99-verify PASSED', out)
    elif code == 2:
        _say('anysda-restore: ⚠ 99-verify: не все проверки измерены; причины выше', err)
    else:
        raise RestoreError(7, 'anysda-restore: 99-verify FAILED — проверь руками; '
                              f'pre-restore снимок: {presnap_final}')


def finish(fail, archive, presnap_final, *, out=None):
    """Итог: пароли не пересинхронизированы - RestoreError(8), иначе блок SUCCESS в out."""
    out = sys.stdout if out is None else out
    if fail:
        raise RestoreError(8, '\nanysda-restore: FAILED — данные восстановлены, но пароли не пересинхронизированы:\n'
                              f'  {fail}\n'
                              f'  Почини причину и прогони ./deploy.sh <стадия> ru; pre-restore снимок: {presnap_final}')
    _say('\nanysda-restore: SUCCESS\n'
         f'  source:       {archive}\n'
         f'  pre-snapshot: {presnap_final}\n'
         '  Существующие клиенты должны переподключаться без переоформления конфигов.', out)
