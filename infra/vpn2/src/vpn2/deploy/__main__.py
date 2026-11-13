"""python3 -m vpn2.deploy [recover|rollback|backup|backup-list|restore ...|<стадия> <группа>]

Зовёт infra/deploy.sh после проверки config.yaml; флаги --development и --manifest он разбирает
сам и отдаёт в VPN2_DEVELOPMENT и VPN2_MANIFEST.
"""
import os
import shutil
import signal
import subprocess
import sys

from vpn2.config import STATE_DIR
from vpn2.deploy import CONFIG, INFRA, REPO, ROOT, die, install_prereqs, migrate_state, report
from vpn2.deploy.run import Deploy, Interrupted


def interrupted(code):
    def handler(_signum, _frame):
        raise Interrupted(code)
    return handler


def dispatch(deploy, argv):
    action = argv[0] if argv else ''
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
    elif action not in ('', 'recover', 'rollback'):
        if len(argv) < 2:
            die('использование: ./deploy.sh <stage> <group>  (пример: ./deploy.sh 30-frontend ru)')
        deploy.stage_nodes(action, [])
        # Стадия entry на экзите (и наоборот) - отказ, а не пустой проход с кодом 0.
        deploy.stage_nodes(action, deploy.expand(argv[1]), refuse_empty=True)
    install_prereqs()
    if action != '99-verify':
        deploy.release_start(action)
    if action in ('', 'recover'):
        code = deploy.do_all()
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
    return 0


def main(argv):
    sys.stdout.reconfigure(line_buffering=True)
    signal.signal(signal.SIGINT, interrupted(130))
    signal.signal(signal.SIGTERM, interrupted(143))
    manifest = os.environ.pop('VPN2_MANIFEST', '')
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
    deploy, code = None, 1
    try:
        deploy = Deploy(os.environ.get('VPN2_DEVELOPMENT') == 'true', manifest)
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
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
