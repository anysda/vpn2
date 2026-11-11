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
DNS_VALUES = {'wireguard': {'wireguard_dns': '10.99.0.1', 'wireguard_subnets': ['198.51.100.0/24']}}


class UfwReconcileTests(unittest.TestCase):
    def run_commands(self, commands, rules):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, 'rules.json')
            state.write_text(json.dumps(rules))
            ufw = Path(directory, 'ufw')
            ufw.write_text(FAKE_UFW)
            ufw.chmod(0o755)
            env = dict(os.environ, PATH=directory + ':' + os.environ['PATH'], UFW_STATE=str(state))
            script = f'source {shlex.quote(str(ROOT / "infra/lib/anysda-svc.sh"))}\n' + commands
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(state.read_text())

    def test_all_listener_stages_remove_owned_stale_rules(self):
        cases = [
            ('27-ikev2', 'ikev2 IKE', '500/udp'),
            ('27-ikev2', 'ikev2 NAT-T', '4500/udp'),
            ('29-openvpn', 'openvpn listener', '1194/tcp'),
        ]
        for stage, comment, desired in cases:
            with self.subTest(stage=stage, comment=comment):
                line = next(line.strip() for line in source(f'infra/scripts/{stage}.sh').splitlines()
                            if comment in line and ('ufw allow' in line or 'ufw_sync_rules' in line))
                old = ['allow', '59999/udp', 'comment', comment]
                manual = ['allow', '59998/udp', 'comment', 'manual ' + comment]
                variables = 'WG_PORT=51820 OVPN_PORT=1194 OVPN_FW_PROTO=tcp HY2_DIRECT_PORT=443 HY2_WARP_PORT=444 HY2_MGMT_PORT=445\n'
                result = self.run_commands(variables + line + '\n' + line, [old, manual])
                self.assertNotIn(old, result)
                self.assertIn(manual, result)
                self.assertEqual(len(result), 2)
                self.assertTrue(any(r[:2] == ['allow', desired] for r in result))

    def test_vpn_roles_remove_owned_stale_rules(self):
        for role, values, comment, desired in [
                ('wireguard', {'wireguard_port': '51820'}, 'wireguard listener', '51820/udp')]:
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
                group = next(g for g in vpn_roles.ufw_groups(role, values | {f'{role}_port': '1'}) if g['comment'] == comment)
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
        block = re.sub(r"\{\{ lookup\('ansible.builtin.env', '(\w+)'\) \}\}", lambda m: ports[m[1]], block)
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

    def test_dns_changes_replace_only_owned_rules(self):
        for stage, service, variable in [('27-ikev2', 'ikev2', 'IKEV2'),
                                          ('29-openvpn', 'openvpn', 'OVPN')]:
            with self.subTest(stage=stage):
                text = source(f'infra/scripts/{stage}.sh')
                if 'ufw_sync_dns' in text:
                    block = next(line for line in text.splitlines() if line.startswith('ufw_sync_dns'))
                else:
                    block = text[text.rindex(f'for subnet in ${variable}_SUBNETS; do'):]
                    block = block[:block.index('\ndone') + len('\ndone')]
                comment = f'{service} → AdGuard DNS'
                old = shlex.split('allow from 198.51.100.0/24 to 192.0.2.53 port 53 proto udp') + ['comment', comment]
                manual = ['allow', '53/tcp', 'comment', 'manual DNS']
                commands = f'{variable}_SUBNETS=198.51.100.0/24 IKEV2_DNS=10.99.0.1\n' + block
                result = self.run_commands(commands, [old, manual])
                self.assertNotIn(old, result)
                self.assertIn(manual, result)
                self.assertEqual(len(result), 3)

    def test_unchanged_and_manual_rules_are_not_rewritten(self):
        rules = [['allow', '51820/udp', 'comment', 'manual listener'],
                 shlex.split('allow from 198.51.100.0/24 to 192.0.2.53 port 53 proto udp') + ['comment', 'DNS']]
        result = self.run_commands("ufw_sync_rules 'wireguard listener' 51820/udp\n"
                                   "ufw_sync_rules DNS 'proto udp from 198.51.100.0/24 to 192.0.2.53 port 53'", rules)
        self.assertEqual(result, rules)


if __name__ == '__main__':
    unittest.main()
