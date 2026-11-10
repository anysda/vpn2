"""Команда `vpn2 backup`: снять зашифрованный бэкап узла или сверить распакованный набор."""
import argparse
import sys

from vpn2.backup import archive, check, env, run


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f'vpn2 backup: {message}\n')


def main(argv):
    parser = _Parser(prog='vpn2 backup', description='Бэкап узла: архив в локальный каталог и, для s3, в бакет.')
    parser.add_argument('--check-bundle', metavar='КАТАЛОГ',
                        help='сверить БД распакованного набора с конфигами VPN и выйти (коды 0, 3, 5)')
    parser.add_argument('--env-file', metavar='ФАЙЛ', default=env.DEFAULT_ENV_FILE,
                        help='файл настроек (по умолчанию %(default)s)')
    args = parser.parse_args(argv)
    if args.check_bundle is not None:
        return check.check_bundle(args.check_bundle)
    try:
        values = env.settings(env.read_env(args.env_file), args.env_file)
    except env.BackupEnvError as exc:
        print(exc, file=sys.stderr)
        return exc.code
    with archive.exit_on_signals():
        return run.create(values, paths=run.Paths(env_file=args.env_file))
