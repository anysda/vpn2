"""Команда `vpn2 control`: служба управления узлом и её привилегированный исполнитель.

`vpn2 control serve [--probe|--expire]` - то же, что launcher службы vpn2-control;
`vpn2 control apply ...` - то же, что /usr/local/sbin/vpn2-apply.
"""
import os
import sys

USAGE = 'vpn2 control: нужно serve [--probe|--expire] или apply <действие> ...\n'


def main(argv):
    if argv and argv[0] in ('-h', '--help'):
        sys.stdout.write(USAGE)
        return 0
    if not argv:
        sys.stderr.write(USAGE)
        return 2
    if argv[0] == 'serve':
        from vpn2.control import server
        server.main(argv[1:])
        return 0
    if argv[0] == 'apply':
        from vpn2.control import apply
        os.umask(0o077)
        apply.main(argv[1:])
        return 0
    sys.stderr.write(USAGE)
    return 2
