"""Execute stage firewall commands against a persistent UFW command double."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from test_deploy import ROOT, source


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
            ('00-bootstrap', 'ssh', '22/tcp'),
            ('00-bootstrap', 'hysteria2 direct', '443/udp'),
            ('00-bootstrap', 'hysteria2 warp', '444/udp'),
            ('00-bootstrap', 'hysteria2 mgmt', '445/udp'),
            ('10-foreign', 'hysteria2 direct', '443/udp'),
            ('10-foreign', 'hysteria2 warp', '444/udp'),
            ('10-foreign', 'hysteria2 mgmt', '445/udp'),
            ('27-ikev2', 'ikev2 IKE', '500/udp'),
            ('27-ikev2', 'ikev2 NAT-T', '4500/udp'),
            ('28-wireguard', 'wireguard listener', '51820/udp'),
            ('29-openvpn', 'openvpn listener', '1194/tcp'),
            ('30-frontend', 'caddy HTTP', '80/tcp'),
            ('30-frontend', 'caddy HTTPS', '443/tcp'),
        ]
        for stage, comment, desired in cases:
            with self.subTest(stage=stage, comment=comment):
                line = next(line.strip() for line in source(f'infra/scripts/{stage}.sh').splitlines()
                            if comment in line and ('ufw allow' in line or 'ufw_sync_rules' in line))
                # Use the exact comment from the old installation, including aliases.
                old_comment = {'hysteria2 mgmt': 'hysteria2 mgmt (monitoring)',
                               'caddy HTTP': 'caddy HTTP панель',
                               'caddy HTTPS': 'caddy HTTPS панель'}.get(comment, comment)
                old = ['allow', '59999/udp', 'comment', old_comment]
                manual = ['allow', '59998/udp', 'comment', 'manual ' + old_comment]
                variables = 'WG_PORT=51820 OVPN_PORT=1194 OVPN_FW_PROTO=tcp HY2_DIRECT_PORT=443 HY2_WARP_PORT=444 HY2_MGMT_PORT=445\n'
                result = self.run_commands(variables + line + '\n' + line, [old, manual])
                self.assertNotIn(old, result)
                self.assertIn(manual, result)
                self.assertEqual(len(result), 2)
                self.assertTrue(any(r[:2] == ['allow', desired] for r in result))

    def test_dns_changes_replace_only_owned_rules(self):
        for stage, service, variable in [('27-ikev2', 'ikev2', 'IKEV2'),
                                          ('28-wireguard', 'wireguard', 'WG'),
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
