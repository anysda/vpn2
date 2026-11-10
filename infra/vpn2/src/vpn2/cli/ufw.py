"""vpn2 ufw sync [--alias X] <comment> [правило ...] - правила UFW одной стадии."""

import argparse

from vpn2 import firewall


def main(argv):
    parser = argparse.ArgumentParser(prog='vpn2 ufw')
    sub = parser.add_subparsers(dest='command', required=True)
    sync = sub.add_parser('sync', help='привести правила с этим комментарием к списку')
    sync.add_argument('--alias', action='append', default=[])
    sync.add_argument('comment')
    sync.add_argument('rules', nargs='*')
    args = parser.parse_args(argv)
    firewall.ufw_reconcile(args.comment, args.rules, args.alias)
    return 0
