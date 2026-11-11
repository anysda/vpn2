"""vpn2 router config|entry-addrs|apply-routes и шаги роли router (sing-box на entry)."""

import importlib
import sys

# имя -> (модуль в vpn2.router, функция)
KINDS = {
    'config': ('config', 'run'),
    'entry-addrs': ('entry_addrs', 'main'),
    'apply-routes': ('apply_routes', 'main'),
}
# Шаги установки: разбирает сам модуль vpn2.router.stage.
STEPS = ('rule-sets', 'base-config', 'restart', 'ssh-config')


def main(argv):
    if argv and argv[0] in STEPS:
        return importlib.import_module('vpn2.router.stage').main(argv)
    if not argv or argv[0] not in KINDS:
        sys.stderr.write('Использование: vpn2 router ' + '|'.join((*KINDS, *STEPS)) + ' [аргументы]\n')
        return 2
    module, function = KINDS[argv[0]]
    return getattr(importlib.import_module(f'vpn2.router.{module}'), function)(argv[1:])
