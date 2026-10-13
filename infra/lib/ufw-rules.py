#!/usr/bin/env python3
"""Reconcile one stage's commented UFW rules, including inactive IPv4/IPv6 rules."""
import argparse
import shlex
import subprocess


def rule_key(words):
    # `show added` expands short port rules on some UFW versions.
    if len(words) == 2 and '/' in words[1]:
        port, proto = words[1].rsplit('/', 1)
        return (words[0], 'from', 'any', 'to', 'any', 'port', port, 'proto', proto)
    if 'proto' in words:
        index = words.index('proto')
        return tuple(words[:index] + words[index + 2:] + words[index:index + 2])
    return tuple(words)


def reconcile(comment, rules, aliases=()):
    desired = [shlex.split('allow ' + rule) for rule in rules]
    wanted = {rule_key(rule) for rule in desired}
    owned = {comment, *aliases}
    result = subprocess.run(['ufw', 'show', 'added'], check=True, capture_output=True, text=True)
    stale = []
    present = set()
    for line in result.stdout.splitlines():
        if not line.startswith('ufw '):
            continue
        words = shlex.split(line)[1:]
        owner = None
        if 'comment' in words:
            index = words.index('comment')
            owner = words[index + 1]
            words = words[:index]
        key = rule_key(words)
        present.add(key)
        if owner in owned and key not in wanted:
            stale.append(words)

    # Open the replacement before closing old ports. Preserve matching manual
    # rules and unchanged rules: no comment takeover or firewall reload.
    for rule in desired:
        if rule_key(rule) not in present:
            subprocess.run(['ufw', *rule, 'comment', comment], check=True)
    for rule in stale:
        # Delete by specification, not number: UFW removes both address families
        # even when disabled, without shifting indices underneath the next delete.
        subprocess.run(['ufw', '--force', 'delete', *rule], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--alias', action='append', default=[])
    parser.add_argument('comment')
    parser.add_argument('rules', nargs='*')
    args = parser.parse_args()
    reconcile(args.comment, args.rules, args.alias)


if __name__ == '__main__':
    main()
