"""python3 -m vpn2.deploy [-key|recover|rollback|backup|backup-list|restore ...|<стадия> <группа>]

Зовёт infra/deploy.sh после проверки config.yaml; флаги --development и --manifest он разбирает
сам и отдаёт в VPN2_DEVELOPMENT и VPN2_MANIFEST.
"""
import os
import shutil
import signal
import subprocess
import sys

from vpn2.config import STATE_DIR, drop_passwords
from vpn2.deploy import (CONFIG, INFRA, REPO, ROOT, admin_key_check, die, install_prereqs, migrate_state, ok,
                         orchestrator_key, read, report, step, svc)
from vpn2.deploy.run import EXIT_FIRST, EXIT_STAGES, Deploy, Interrupted
from vpn2.manager import Progress

# Действия заданий менеджера установки и число их аргументов с действием: exit-add <тег>, exit-remove <тег> <адрес>.
EXIT_ACTIONS = {'exit-add': 2, 'exit-remove': 3}


def interrupted(code):
    def handler(_signum, _frame):
        raise Interrupted(code)
    return handler


def dispatch(deploy, argv):
    action, scope = (argv[0] if argv else ''), ''
    # Ошибка в аргументах выпуск не открывает: иначе журнал остался бы в applying до recover.
    if action == 'backup':
        return report.backup()
    if action == 'backup-list':
        return subprocess.run(['/usr/local/bin/anysda-backup-list.sh']).returncode
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
    # Автообновления свежей ОС стартуют в первые минуты после загрузки: пауза - до любого apt деплоя.
    if action in ('', 'recover') and os.path.isdir('/run/systemd/system'):
        if code := svc('nftables_preserve_rules'):
            return code
        step('автообновления entry на паузу до конца деплоя')
        if code := svc('apt_auto_pause', tag='ru'):
            return code
    install_prereqs()
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
    return 0


def main(argv):
    sys.stdout.reconfigure(line_buffering=True)
    signal.signal(signal.SIGINT, interrupted(130))
    signal.signal(signal.SIGTERM, interrupted(143))
    manifest = os.environ.pop('VPN2_MANIFEST', '')
    if argv == ['-key']:
        key = orchestrator_key(create=True)
        print(read(f'{key}.pub'))
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
    deploy, code, progress = None, 1, Progress.from_env()
    try:
        deploy = Deploy(os.environ.get('VPN2_DEVELOPMENT') == 'true', manifest)
        deploy.progress = progress
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
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
