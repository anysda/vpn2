"""Команда `vpn2 backup-list`: список доступных бэкапов (локально и в S3)."""
import argparse
import sys

from vpn2.backup import env, listing


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f'vpn2 backup-list: {message}\n')


def main(argv):
    parser = _Parser(prog='vpn2 backup-list', description='Список бэкапов: имя, метка UTC, размер; для s3 ещё и содержимое бакета.')
    parser.add_argument('--env-file', metavar='ФАЙЛ', default=env.DEFAULT_ENV_FILE,
                        help='файл настроек (по умолчанию %(default)s)')
    args = parser.parse_args(argv)
    try:
        values = env.settings(env.read_env(args.env_file), args.env_file)
    except env.BackupEnvError as exc:
        text = f'anysda-backup-list: {args.env_file} не доступен' if exc.code == 2 else str(exc)
        print(text, file=sys.stderr)
        return exc.code
    return listing.render(values)
