"""python3 -m vpn2.deploy [-key|recover|rollback|backup|backup-list|restore ...|<стадия> <группа>]

Зовёт infra/deploy.sh после проверки config.yaml; флаги --development и --manifest он разбирает
сам и отдаёт в VPN2_DEVELOPMENT и VPN2_MANIFEST.
"""
import contextlib
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys

from vpn2 import creds
from vpn2.config import STATE_DIR, drop_passwords
from vpn2.deploy import (CONFIG, INFRA, REPO, ROOT, G, E, admin_key_check, die, finish_secrets, install_prereqs,
                         installed, migrate_state, ok, orchestrator_key, pub, read, report, step, svc, take_secrets)
from vpn2.deploy.run import EXIT_FIRST, EXIT_STAGES, Deploy, Interrupted
from vpn2.manager import Progress

# Действия заданий менеджера установки и число их аргументов с действием: exit-add <тег>, exit-remove <тег> <адрес>.
EXIT_ACTIONS = {'exit-add': 2, 'exit-remove': 3}


def interrupted(code):
    def handler(_signum, _frame):
        raise Interrupted(code)
    return handler


def dispatch(deploy, argv, new_server=None):
    action, scope = (argv[0] if argv else ''), ''
    # Ошибка в аргументах выпуск не открывает: иначе журнал остался бы в applying до recover.
    if action == 'backup':
        return report.backup()
    if action == 'backup-list':
        return subprocess.run(['/usr/local/bin/anysda-backup-list.sh']).returncode
    if action == 'restore' and '--dry-run' not in argv[1:] and new_server is None:
        new_server = not installed()
        argv = ['restore', *report.restore_input(deploy, argv[1:], new_server)]
    if action == 'restore' and new_server:
        # Новый сервер: ключи и секреты архива - до ключа оркестратора, затем деплой и restore архива поверх.
        report.fresh_secrets(deploy, argv[1])
        flags = [arg for arg in argv[2:] if arg != '--force']
        return dispatch(deploy, []) or dispatch(deploy, ['restore', argv[1], '--force', *flags], new_server=False)
    if action == 'restore':
        if '--dry-run' in argv[1:]:
            return report.restore(deploy, argv[1:])[0]
        if code := report.restore_preflight(deploy, argv[1:]):
            return code
    elif action in EXIT_ACTIONS:
        # Задание менеджера установки (vpn2.manager): config.yaml уже с новым составом экзитов.
        if len(argv) != EXIT_ACTIONS[action] or (action == 'exit-add') != (argv[1] in deploy.exit_tags()):
            die(f'{action}: задание менеджера установки, не для запуска руками')
    elif action not in ('', 'recover', 'rollback'):
        if len(argv) < 2:
            die('использование: ./deploy.sh <stage> <group>  (пример: ./deploy.sh 30-frontend ru)')
        deploy.stage_nodes(action, [])
        # Стадия entry на экзите (и наоборот) - отказ, а не пустой проход с кодом 0.
        scope = deploy.stage_nodes(action, deploy.expand(argv[1]), refuse_empty=True)
    # restore и rollback возвращают уже стоявшее: вход по паролю ими не закрывается.
    if action not in ('restore', 'rollback'):
        admin_key_check(deploy.env())
    orchestrator_key(create=True)
    if deploy.progress:
        deploy.progress.plan(['preflight', *({'exit-add': (*EXIT_FIRST, *EXIT_STAGES), 'exit-remove': (*EXIT_STAGES, 'cleanup')}
                                             .get(action) or [stage.name for stage in deploy.stages])])
    if action in ('', 'recover', *EXIT_ACTIONS):
        report.banner(deploy)
        deploy.preflight()
        if action == 'exit-add':
            deploy.clean_new_exit(argv[1])
    # Автообновления свежей ОС стартуют в первые минуты после загрузки: пауза - до любого apt деплоя.
    if action in ('', 'recover') and os.path.isdir('/run/systemd/system'):
        if code := svc('nftables_preserve_rules'):
            return code
        step('автообновления entry на паузу до конца деплоя')
        if code := svc('apt_auto_pause', tag='ru'):
            return code
    install_prereqs()
    if action in report.KEY_ACTIONS and deploy.backup_key is None:
        deploy.backup_key = report.backup_key_start(bool(deploy.progress))
    if action != '99-verify':
        deploy.release_start(action, argv[1] if action == 'exit-add' else scope)
    if action in ('', 'recover'):
        code = deploy.do_all()
    elif action == 'exit-add':
        return deploy.add_exit(argv[1])
    elif action == 'exit-remove':
        return deploy.remove_exit(argv[1], argv[2])
    elif action == 'restore':
        code = report.restore(deploy, argv[1:])[0]
    elif action == 'rollback':
        code = subprocess.run([ROOT / 'venv/bin/vpn2', 'containers', 'rollback']).returncode or subprocess.run(
            [sys.executable, '-m', 'vpn2.cli', 'state', 'phase', '--phase', 'rolled-back'],
            env={**os.environ, 'PYTHONPATH': '/tmp/anysda'}).returncode
    else:
        code = deploy.run_stage(action, argv[1])
        if action == '30-frontend' and argv[1] in ('ru', 'all'):
            report.sso_notice(deploy)
    if code:
        return code
    deploy.release_complete()
    # Пароли root из config.yaml 1.2.0 больше не нужны: вход по ключу на все узлы прошёл, деплой - тоже.
    if action in ('', 'recover') and drop_passwords(CONFIG):
        ok('пароли root entry и экзитов убраны из config.yaml: вход на серверы только по ключу')
    # Секреты уже в хранилище (take_secrets до стадий): успешный прогон стирает их из config.yaml.
    if finish_secrets(panel=action in ('', 'recover') or (action == '30-frontend' and argv[1] in ('ru', 'all'))):
        ok('секреты перенесены в зашифрованное хранилище entry и стёрты из config.yaml')
    return 0


def panel_admin(db='/var/lib/anysda-vpn2/db.sqlite'):
    """В базе панели уже есть админ: его пароль деплой без secrets.admin_password не трогает."""
    if not os.path.exists(db):
        return False
    try:
        with contextlib.closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True)) as base:
            return base.execute('SELECT count(*) FROM users').fetchone()[0] > 0
    except sqlite3.Error as error:
        # Занятая или битая база - не повод задать новый пароль: админ, может быть, есть.
        return 'no such table' not in str(error)


def main(argv):
    sys.stdout.reconfigure(line_buffering=True)
    signal.signal(signal.SIGINT, interrupted(130))
    signal.signal(signal.SIGTERM, interrupted(143))
    manifest = os.environ.pop('VPN2_MANIFEST', '')
    if argv == ['-key']:
        orchestrator_key(create=True)
        print(read(pub()))
        print('Положи эту строку в /root/.ssh/authorized_keys на каждом экзите, затем заполни config.yaml '
              'и запусти ./deploy.sh -yaml', file=sys.stderr)
        return 0
    os.environ.update(DEPLOY_ROOT=str(INFRA), VPN2_INFRA=str(INFRA), VPN2_SOURCE_ROOT=str(REPO), VPN2_CONFIG=str(CONFIG),
                      VPN2_INVENTORY_SECRETS='show', VPN2_RELEASE_MANIFEST='/tmp/anysda/release-manifest.json')
    try:
        migrate_state()
    except OSError as error:
        print(f'✗ состояние деплоя не перенеслось из infra/state в {STATE_DIR}: {error}', file=sys.stderr)
        return 1
    if subprocess.run([sys.executable, '-B', INFRA / 'ansible/inventory.py', '--prepare']).returncode:
        return 1
    # Каталог окружений прежних выпусков (с секретами) не читается никем.
    shutil.rmtree(REPO / 'infra/envs', ignore_errors=True)
    try:
        password, generated = take_secrets(), ''
    except (creds.CredsError, OSError) as error:
        print(f'✗ секреты не перенесены в хранилище entry: {error}', file=sys.stderr)
        return 1
    # Пароль админа доходит до стадии панели окружением. Не задан, а базы панели ещё нет - новый.
    if not password and argv[:1] in ([], ['recover']) and not panel_admin():
        password = generated = secrets.token_urlsafe(13)[:18]
    # restore нового сервера: панели деплоя нужен пароль, админ с паролем придёт из архива - этот не показывается.
    if not password and argv[:1] == ['restore'] and not installed():
        password = secrets.token_urlsafe(13)[:18]
    if password:
        os.environ['ADMIN_PASSWORD'] = password
    deploy, code, progress = None, 1, Progress.from_env()
    try:
        deploy = Deploy(os.environ.get('VPN2_DEVELOPMENT') == 'true', manifest)
        deploy.progress = progress
        deploy.admin_generated = bool(generated)
        deploy.backup_key = deploy.restore_secret = deploy.restore_pulled = None
        code = dispatch(deploy, argv)
    except subprocess.CalledProcessError as error:
        code = error.returncode
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1
        if isinstance(error.code, str):
            print(error.code, file=sys.stderr)
    finally:
        if deploy:
            deploy.close(code)
        if progress:
            progress.finish(code, deploy and deploy.result)
        # Вывод задания менеджера ложится в журнал задания на диске: секретов в нём нет.
        if generated and panel_admin() and not progress:
            print(f'\n  {G}Пароль панели: {generated}{E}  (показан один раз и нигде не хранится)\n', flush=True)
        if deploy and deploy.backup_key is not None and not progress:
            try:
                report.show_backup_key(deploy.backup_key)
            except (creds.CredsError, OSError) as error:
                print(f'✗ ключ бэкапов не показан: {error}', file=sys.stderr)
                code = code or 1
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
