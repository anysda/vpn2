"""Консольная команда vpn2: одна таблица команд.

Обработчик получает аргументы после имени команды и возвращает код выхода.
Модуль обработчика грузится только при вызове своей команды: сторож экзитов
читает окружение при импорте, а экзит не несёт зависимостей entry.
"""
import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path

# команда, обработчик "модуль:функция"
COMMANDS = (
    ('adguard', 'vpn2.adguard.stage:main'),
    ('backup', 'vpn2.cli:backup'),
    ('backup-list', 'vpn2.cli:backup_list'),
    ('containers', 'vpn2.cli:containers'),
    ('control', 'vpn2.cli:control'),
    ('emergency', 'vpn2.emergency.__main__:main'),
    ('host', 'vpn2.cli.host:main'),
    ('nft', 'vpn2.cli:nft'),
    ('release', 'vpn2.release:main'),
    ('restore', 'vpn2.cli:restore'),
    ('router', 'vpn2.cli:router'),
    ('snapshot', 'vpn2.cli:snapshot'),
    ('verify', 'vpn2.cli:verify'),
    ('version', 'vpn2.cli:version'),
    ('watchdog', 'vpn2.cli:watchdog'),
    ('zapret', 'vpn2.zapret.stage:main'),
)

# подкоманда -> "модуль:функция"; функция получает остаток аргументов
WATCHDOGS = {
    'failover': 'vpn2.watchdog.failover:main',
    'failover-env': 'vpn2.watchdog.failover_env:main',
    'warp': 'vpn2.watchdog.warp:main',
    'yt': 'vpn2.watchdog.yt:run',
}
ROUTER = {
    'entry-addrs': 'vpn2.router.entry_addrs:main',
    'apply-routes': 'vpn2.router.apply_routes:main',
}

RESTORE_USAGE = '''Использование: vpn2 restore [АРХИВ|latest] [--force|-f] [--dry-run] [--allow-incompatible]
Восстановить узел из архива (по умолчанию - самый свежий, локальный или из S3).
  --force, -f            перетереть непустую базу с клиентами
  --dry-run              только назвать архив, ничего не менять
  --allow-incompatible   восстановить архив другой версии формата или схемы БД
'''
SNAPSHOT_TOOL = Path('infra/tools/snapshot.py')
CONTROL_USAGE = 'vpn2 control: нужно serve [--probe|--expire] или apply <действие> ...\n'


def _resolve(target):
    module, _, function = target.partition(':')
    return getattr(importlib.import_module(module), function)


class _Parser(argparse.ArgumentParser):
    """Ошибка разбора - одна строка с именем команды и код 2."""

    def error(self, message):
        self.exit(2, f'{self.prog}: {message}\n')


def _backup_settings(env_file):
    from vpn2.backup import env
    return env.settings(env.read_env(env_file), env_file)


def _env_file_argument(parser):
    from vpn2.backup import env
    parser.add_argument('--env-file', metavar='ФАЙЛ', default=env.DEFAULT_ENV_FILE,
                        help='файл настроек (по умолчанию %(default)s)')


def backup(argv):
    from vpn2.backup import archive, check, env, run
    parser = _Parser(prog='vpn2 backup', description='Бэкап узла: архив в локальный каталог и, для s3, в бакет.')
    parser.add_argument('--check-bundle', metavar='КАТАЛОГ', nargs='?', const='',
                        help='сверить БД распакованного набора с конфигами VPN и выйти (коды 0, 3, 5)')
    _env_file_argument(parser)
    args = parser.parse_args(argv)
    if args.check_bundle is not None:
        return check.check_bundle(args.check_bundle)
    try:
        values = _backup_settings(args.env_file)
    except env.BackupEnvError as exc:
        print(exc, file=sys.stderr)
        return exc.code
    with archive.exit_on_signals():
        return run.create(values, paths=run.Paths(env_file=args.env_file))


def backup_list(argv):
    from vpn2.backup import env, listing
    parser = _Parser(prog='vpn2 backup-list',
                     description='Список бэкапов: имя, метка UTC, размер; для s3 ещё и содержимое бакета.')
    _env_file_argument(parser)
    args = parser.parse_args(argv)
    try:
        values = _backup_settings(args.env_file)
    except env.BackupEnvError as exc:
        text = f'anysda-backup-list: {args.env_file} не доступен' if exc.code == 2 else str(exc)
        print(text, file=sys.stderr)
        return exc.code
    return listing.render(values)


def restore(argv):
    if argv and argv[0] in ('-h', '--help'):
        sys.stdout.write(RESTORE_USAGE)
        return 0
    from vpn2.backup import restore as module
    return module.main(argv)


def containers(argv):
    """vpn2 containers monitoring|telegram|panel|rollback - контейнеры entry под compose."""
    from vpn2 import containers as stage
    parser = argparse.ArgumentParser(prog='vpn2 containers')
    parser.add_argument('command', choices=[*stage.STAGES, 'rollback'])
    args = parser.parse_args(argv)
    try:
        if args.command == 'rollback':
            return 0 if stage.rollback() else 1
        if os.environ.get('HOST_TAG') != 'ru':
            print(f'[{os.environ.get("HOST_TAG")}] {args.command}: только для entry - пропуск')
            print('changed=0')
            return 0
        changed = stage.run(args.command, os.environ)
    except stage.ERRORS as error:
        if str(error):
            print(f'vpn2 containers {args.command}: {error}', file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f'vpn2 containers {args.command}: {error.cmd[0]}: код {error.returncode}', file=sys.stderr)
        return 1
    print(f'changed={int(bool(changed))}')
    return 0


def control(argv):
    """`serve` - служба vpn2-control, `apply` - то же, что /usr/local/sbin/vpn2-apply."""
    if argv and argv[0] in ('-h', '--help'):
        sys.stdout.write(CONTROL_USAGE)
        return 0
    if argv and argv[0] == 'serve':
        from vpn2.control import server
        server.main(argv[1:])
        return 0
    if argv and argv[0] == 'apply':
        from vpn2.control import apply
        os.umask(0o077)
        apply.main(argv[1:])
        return 0
    sys.stderr.write(CONTROL_USAGE)
    return 2


def nft(argv):
    """vpn2 nft yt-quic <goog.json> - таблица nft блокировки QUIC к Google в stdout."""
    from vpn2 import firewall
    if len(argv) != 2 or argv[0] != 'yt-quic':
        sys.stderr.write('Использование: vpn2 nft yt-quic <goog.json>\n')
        return 2
    try:
        v4, v6 = firewall.load_google_prefixes(argv[1])
    except (OSError, ValueError) as error:
        sys.stderr.write(f'ERROR: {error}\n')
        return 1
    sys.stdout.write(firewall.google_quic_table(v4, v6))
    return 0


def router(argv):
    if not argv or argv[0] not in ROUTER:
        sys.stderr.write('Использование: vpn2 router ' + '|'.join(ROUTER) + ' [аргументы]\n')
        return 2
    return _resolve(ROUTER[argv[0]])(argv[1:])


def watchdog(argv):
    if not argv or argv[0] not in WATCHDOGS:
        sys.stderr.write('Использование: vpn2 watchdog ' + '|'.join(WATCHDOGS) + ' [аргументы]\n')
        return 2
    run = _resolve(WATCHDOGS[argv[0]])
    if argv[0] == 'failover':
        return run()
    return run(argv[1:])


def verify(argv):
    """Код 0 - всё работает, 1 - отказ, 2 - часть не проверена; узел приходит окружением стадии."""
    from vpn2.verify import stage
    parser = argparse.ArgumentParser(prog='vpn2 verify', description='Проверка узла после деплоя')
    parser.add_argument('--target', metavar='узел', help='тег узла вместо HOST_TAG: ru или тег экзита')
    parser.add_argument('--traffic', action='store_true', help='только настоящий трафик контрольным клиентом')
    args = parser.parse_args(argv)
    env = dict(os.environ)
    if args.target:
        env['HOST_TAG'] = args.target
    return stage.run(env, traffic_only=args.traffic)


def version(argv):
    del argv
    from importlib.metadata import version as installed
    print(installed('vpn2'))


def snapshot(argv):
    """Слепки стенда CI снимает и сверяет infra/tools/snapshot.py из checkout."""
    if not SNAPSHOT_TOOL.is_file():
        print(f'vpn2 snapshot: нет {SNAPSHOT_TOOL} - запускайте из корня checkout', file=sys.stderr)
        return 2
    return subprocess.run([sys.executable, '-B', str(SNAPSHOT_TOOL), *argv], check=False).returncode


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    commands = dict(COMMANDS)
    usage = 'Команды: ' + ', '.join(sorted(commands)) + '\n'
    if not argv:
        sys.stderr.write('vpn2: нужна команда\n' + usage)
        return 2
    if argv[0] in ('-h', '--help'):
        sys.stdout.write('Использование: vpn2 <команда> [аргументы]\n' + usage)
        return 0
    if argv[0] not in commands:
        sys.stderr.write(f'vpn2: неизвестная команда {argv[0]}\n' + usage)
        return 2
    try:
        return _resolve(commands[argv[0]])(argv[1:]) or 0
    except Exception as exc:
        sys.stderr.write(f'vpn2 {argv[0]}: {exc!r}\n')
        return 1
