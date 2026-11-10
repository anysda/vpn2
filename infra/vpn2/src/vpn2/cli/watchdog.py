"""vpn2 watchdog failover|warp|yt|hy2 - сторожа entry и экзита."""

import importlib
import sys

# имя -> (модуль в vpn2.watchdog, функция): модуль грузится только свой,
# сторож экзитов читает окружение при импорте.
KINDS = {
    'failover': ('failover', 'main'),
    'warp': ('warp', 'main'),
    'yt': ('yt', 'run'),
    'hy2': ('hy2_bandwidth', 'main'),
}


def main(argv):
    if not argv or argv[0] not in KINDS:
        sys.stderr.write('Использование: vpn2 watchdog ' + '|'.join(KINDS) + ' [аргументы]\n')
        return 2
    module, function = KINDS[argv[0]]
    run = getattr(importlib.import_module(f'vpn2.watchdog.{module}'), function)
    if argv[0] == 'failover':
        return run()
    return run(argv[1:])
