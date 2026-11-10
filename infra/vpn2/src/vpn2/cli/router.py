"""vpn2 router config|entry-addrs|apply-routes - роутер entry (sing-box)."""

import importlib
import sys

# имя -> (модуль в vpn2.router, функция)
KINDS = {
    'config': ('config', 'run'),
    'entry-addrs': ('entry_addrs', 'main'),
    'apply-routes': ('apply_routes', 'main'),
}


def main(argv):
    if not argv or argv[0] not in KINDS:
        sys.stderr.write('Использование: vpn2 router ' + '|'.join(KINDS) + ' [аргументы]\n')
        return 2
    module, function = KINDS[argv[0]]
    return getattr(importlib.import_module(f'vpn2.router.{module}'), function)(argv[1:])
