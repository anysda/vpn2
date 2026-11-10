"""Команда `vpn2 snapshot`: снять слепок хоста или сверить два слепка (только stdlib)."""
import argparse
import json
import sys

from vpn2 import snapshot

_ERRORS = (OSError, ValueError, EOFError, ImportError, snapshot.SnapshotError)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f'vpn2 snapshot: {message}\n')


def _collect_parser():
    parser = _Parser(prog='vpn2 snapshot', description='Снять слепок хоста. Сверка: vpn2 snapshot diff СТАРЫЙ НОВЫЙ.')
    parser.add_argument('--out', metavar='ФАЙЛ', help='записать слепок в файл (.json или .json.gz), иначе JSON в stdout')
    parser.add_argument('--baseline', metavar='ФАЙЛ', help='вычесть слепок-основу, оставить только отличия')
    parser.add_argument('--root', metavar='КАТАЛОГ', default='/', help='префикс чтения файлов (по умолчанию /)')
    return parser


def _diff_parser():
    parser = _Parser(prog='vpn2 snapshot diff', description='Сверить два слепка; расхождения дают код 1.')
    parser.add_argument('--ignore', metavar='ФАЙЛ', help='YAML-список правил игнорирования')
    parser.add_argument('old', metavar='СТАРЫЙ', help='эталонный слепок')
    parser.add_argument('new', metavar='НОВЫЙ', help='проверяемый слепок')
    return parser


def _load(path):
    data = snapshot.load(path)
    if not isinstance(data, dict):
        raise snapshot.SnapshotError(f'{path}: слепок не объект')
    return data


def _collect(args):
    result = snapshot.collect(args.root)
    if args.baseline:
        result = snapshot.subtract(result, _load(args.baseline))
    if args.out:
        snapshot.dump(result, args.out)
    else:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=1))
    return 0


def _diff(args):
    old, new = _load(args.old), _load(args.new)
    ignore = snapshot.load_ignore(args.ignore) if args.ignore else []
    lines = snapshot.diff(old, new, ignore)
    for rule in snapshot.unused_ignore(old, new, ignore):
        print(f'ignore не сработал: {rule["section"]} {rule["name"]} {rule["field"]}', file=sys.stderr)
    if not lines:
        print('слепки совпадают')
        return 0
    for line in lines:
        print(line)
    print(f'расхождений: {len(lines)}')
    return 1


def main(argv):
    argv = list(argv)
    try:
        if argv[:1] == ['diff']:
            return _diff(_diff_parser().parse_args(argv[1:]))
        return _collect(_collect_parser().parse_args(argv))
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 2
    except _ERRORS as error:
        text = ' '.join(str(error).split()) or type(error).__name__
        print(f'vpn2 snapshot: {text}', file=sys.stderr)
        return 2
