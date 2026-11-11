"""vpn2 emergency <команда> - службы и шаги аварийного контура."""

import importlib
import sys

# команда -> модуль: грузится только свой, агент на экзите остаётся малым.
COMMANDS = {
    'agent': 'agent',
    'hy2': 'hy2',
    'snapshot-sender': 'snapshot_sender',
    'provision': 'provision',
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write('Использование: vpn2 emergency ' + '|'.join(COMMANDS) + ' [аргументы]\n')
        return 2
    module = importlib.import_module(f'{__package__}.{COMMANDS[argv[0]]}')
    return module.main(argv[1:])


if __name__ == '__main__':
    raise SystemExit(main())
