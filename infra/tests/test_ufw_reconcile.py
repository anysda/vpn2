"""Execute stage firewall commands against a persistent UFW command double."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml

from test_deploy import ROOT, source
import vpn_roles


FAKE_UFW = '''#!/usr/bin/env python3
import json, os, shlex, sys
from pathlib import Path
p = Path(os.environ['UFW_STATE'])
rules = json.loads(p.read_text())
args = sys.argv[1:]
if args == ['show', 'added']:
    print("Added user rules (see 'ufw status' for running firewall):")
    for r in rules:
        print(shlex.join(['ufw', *r]))
    sys.exit(0)
if args[:2] == ['--force', 'delete']:
    target = args[2:]
    rules = [r for r in rules if r[:len(target)] != target]
elif args[0] == 'allow':
    # UFW updates the comment on an existing rule with the same specification.
    spec = args[:args.index('comment')]
    rules = [r for r in rules if r[:len(spec)] != spec]
    rules.append(args)
else:
    raise RuntimeError(args)
p.write_text(json.dumps(rules))
'''

# Роли VPN-серверов: значения переменных для групп DNS.
DNS_VALUES = {role: {f'{role}_dns': '10.99.0.1', f'{role}_subnets': ['198.51.100.0/24']}
              for role in ('wireguard', 'openvpn', 'ikev2')}


class UfwReconcileTests(unittest.TestCase):
    def test_vpn_roles_remove_owned_stale_rules(self):
        for role, values, comment, desired in [
                ('wireguard', {'wireguard_port': '51820'}, 'wireguard listener', '51820/udp'),
                ('openvpn', {'openvpn_port': '1194', 'openvpn_ufw_proto': 'tcp'}, 'openvpn listener', '1194/tcp'),
                ('ikev2', {}, 'ikev2 IKE', '500/udp'), ('ikev2', {}, 'ikev2 NAT-T', '4500/udp')]:
            with self.subTest(role=role, comment=comment):
                group = next(g for g in vpn_roles.ufw_groups(role, values | DNS_VALUES[role]) if g['comment'] == comment)
                old = ['allow', '59999/udp', 'comment', comment]
                manual = ['allow', '59998/udp', 'comment', 'manual ' + comment]
                result = vpn_roles.ufw_apply([group], [old, manual], FAKE_UFW)
                self.assertNotIn(old, result)
                self.assertIn(manual, result)
                self.assertEqual(len(result), 2)
                self.assertTrue(any(r[:2] == ['allow', desired] for r in result))

    def test_vpn_roles_replace_only_owned_dns_rules(self):
        for role, values in DNS_VALUES.items():
            with self.subTest(role=role):
                comment = f'{role} → AdGuard DNS'
                group = next(g for g in vpn_roles.ufw_groups(role, values | {f'{role}_port': '1', f'{role}_ufw_proto': 'udp'}) if g['comment'] == comment)
                old = shlex.split('allow from 198.51.100.0/24 to 192.0.2.53 port 53 proto udp') + ['comment', comment]
                manual = ['allow', '53/tcp', 'comment', 'manual DNS']
                result = vpn_roles.ufw_apply([group], [old, manual], FAKE_UFW)
                self.assertNotIn(old, result)
                self.assertIn(manual, result)
                self.assertEqual(len(result), 3)
                for proto in ('udp', 'tcp'):
                    self.assertIn(shlex.split(f'allow proto {proto} from 198.51.100.0/24 to 10.99.0.1 port 53')
                                  + ['comment', comment], result)

    def test_base_role_removes_owned_stale_rules(self):
        # Первая стадия ведёт ufw шагом пакета: группы роли base идут в ufw_reconcile.
        defaults = source('infra/ansible/roles/base/defaults/main.yml')
        block = defaults.split('base_ufw_exit:\n', 1)[1].split('base_ufw:', 1)[0]
        ports = {'HY2_DIRECT_PORT': '443', 'HY2_WARP_PORT': '444', 'HY2_MGMT_PORT': '445'}
        block = re.sub(r"\{\{ vpn2_env\.(\w+) \| default\(''\) \}\}", lambda m: ports[m[1]], block)
        groups = {group['comment']: group for group in yaml.safe_load(block)}
        for comment, desired in [('ssh', '22/tcp'), ('hysteria2 direct', '443/udp'),
                                 ('hysteria2 warp', '444/udp'), ('hysteria2 mgmt', '445/udp')]:
            with self.subTest(comment=comment), tempfile.TemporaryDirectory() as directory:
                group = groups[comment]
                self.assertEqual(group['rules'], [desired])
                old_comment = group.get('aliases', [comment])[0]
                old = ['allow', '59999/udp', 'comment', old_comment]
                manual = ['allow', '59998/udp', 'comment', 'manual ' + old_comment]
                state = Path(directory, 'rules.json')
                state.write_text(json.dumps([old, manual]))
                ufw = Path(directory, 'ufw')
                ufw.write_text(FAKE_UFW)
                ufw.chmod(0o755)
                code = ('import json, sys; from vpn2 import firewall; g = json.loads(sys.argv[1]); '
                        '[firewall.ufw_reconcile(g["comment"], g["rules"], g.get("aliases", ())) for _ in range(2)]')
                env = dict(os.environ, PATH=directory + ':' + os.environ['PATH'], UFW_STATE=str(state),
                           PYTHONPATH=str(ROOT / 'infra/vpn2/src'))
                subprocess.run([sys.executable, '-B', '-c', code, json.dumps(group)], env=env, check=True,
                               capture_output=True, text=True, timeout=30)
                result = json.loads(state.read_text())
                self.assertNotIn(old, result)
                self.assertIn(manual, result)
                self.assertEqual(len(result), 2)
                self.assertTrue(any(r[:2] == ['allow', desired] for r in result))

    def test_unchanged_and_manual_rules_are_not_rewritten(self):
        rules = [['allow', '51820/udp', 'comment', 'manual listener'],
                 shlex.split('allow from 198.51.100.0/24 to 192.0.2.53 port 53 proto udp') + ['comment', 'DNS']]
        result = vpn_roles.ufw_apply([{'comment': 'wireguard listener', 'rules': ['51820/udp']},
                                      {'comment': 'DNS', 'rules': ['proto udp from 198.51.100.0/24 to 192.0.2.53 port 53']}],
                                     rules, FAKE_UFW)
        self.assertEqual(result, rules)


if __name__ == '__main__':
    unittest.main()
