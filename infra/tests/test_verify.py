"""Run the real verify script with fake host commands; no root or services needed."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
MOCK = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = ' '.join(sys.argv[1:])
fault = os.environ['FAULT']
if fault == 'bare' and name not in ('sysctl', 'ufw', 'ss', 'ip'):
    sys.exit(1)
if fault == name + '-error':
    sys.exit(1)
if name == 'systemctl':
    sys.exit(3 if fault == sys.argv[-1] else 0)
elif name == 'sysctl':
    print(0 if fault == 'forwarding' else 1)
elif name == 'ufw':
    print('Status: inactive' if fault == 'firewall' else 'Status: active')
elif name == 'curl':
    url = sys.argv[-1]
    if fault == url:
        sys.exit(22)
    if url.endswith('/targets'):
        if fault == 'bad-json':
            print('{')
        else:
            targets = [{'labels': {'host': host}, 'health': 'up'} for host in ('ru', 'exit')]
            if fault == 'empty-targets':
                targets = []
            elif fault == 'missing-target':
                targets.pop()
            elif fault == 'down-target':
                targets[1]['health'] = 'down'
            print(json.dumps({'status': 'success', 'data': {'activeTargets': targets}}))
elif name == 'ip':
    if fault == 'bare':
        sys.exit(0)
    if args == 'rule list' and fault != 'tproxy-rule':
        print('100: from all fwmark 0x42 lookup 101')
    elif args == 'route show table 101' and fault != 'tproxy-route':
        print('local default dev lo scope host')
    elif args == 'link show xfrm0':
        sys.exit(1 if fault == 'xfrm' else 0)
elif name == 'ss':
    if fault == 'bare':
        sys.exit(0)
    ports = ('7898', '500', '4500', '443', '8443', '9443')
    for port in ports:
        if fault != 'port-' + port and (':' + port in args or ':' not in args):
            print('UNCONN 0 0 127.0.0.1:' + port + ' 0.0.0.0:*')
elif name == 'swanctl' and fault != 'ikev2-conn':
    print('anysda-ikev2: IKEv2')
if fault == name + '-partial':
    sys.exit(1)
'''


class VerifyTest(unittest.TestCase):
    def run_verify(self, fault='', role='ru', youtube='off'):
        with tempfile.TemporaryDirectory(prefix='verify-test-') as directory:
            root = Path(directory)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            for name in ('systemctl', 'sysctl', 'ufw', 'curl', 'ip', 'ss', 'ssh', 'swanctl'):
                command = bin_dir / name
                command.write_text(MOCK)
                command.chmod(0o755)
            # File checks still run for real, against disposable fixture paths.
            networkd = root / 'networkd.conf'
            if fault != 'networkd':
                networkd.touch()
            status = root / 'status.json'
            if fault != 'missing-status':
                age = {'stale-status': 121, 'future-status': -60}.get(fault, 10)
                status.write_text('{' if fault == 'bad-status' else json.dumps({'updated': time.time() - age}))
            yt = root / 'yt-check'
            if fault != 'missing-yt':
                yt.write_text('#!/bin/sh\nexit ' + ('1' if fault == 'yt-failed' else '0') + '\n')
                yt.chmod(0o755)
            failover = root / 'yt-failover'
            failover.write_text('#!/bin/sh\nexit ' + ('1' if fault == 'yt-failover' else '0') + '\n')
            failover.chmod(0o755)
            ref = os.environ.get('VERIFY_REF')
            source = subprocess.check_output(
                ['git', 'show', f'{ref}:infra/scripts/99-verify.sh'], cwd=ROOT, text=True,
            ) if ref else (ROOT / 'infra/scripts/99-verify.sh').read_text()
            source = source.replace('/etc/systemd/networkd.conf.d/60-anysda-foreign.conf', str(networkd))
            source = source.replace('/etc/anysda/ikev2-status.json', str(status))
            source = source.replace('/usr/local/bin/anysda-yt-check', str(yt))
            source = source.replace('/usr/local/bin/anysda-yt-failover', str(failover))
            source = source.replace('/tmp/yt-check.out', str(root / 'yt-check.out'))
            script = root / 'verify.sh'
            script.write_text(source)
            env = {**os.environ, 'PATH': f'{bin_dir}:' + os.environ['PATH'],
                   'HOST_TAG': role, 'MGMT_IP': '198.51.100.254', 'EXIT_TAGS': '' if fault == 'bare' else 'exit',
                   'YT_ROUTE': youtube, 'FAULT': fault,
                   'HY2_MGMT_PORT': '' if fault == 'missing-mgmt-port' else '9443'}
            env.pop('BASH_ENV', None)
            return subprocess.run(['bash', str(script)], env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)

    def test_bare_host(self):
        result = self.run_verify('bare')
        print(result.stdout, end='')
        print(f'bare host verify exit={result.returncode}')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('sing-box', result.stdout)
        self.assertIn('tproxy', result.stdout)
        self.assertIn('wg-quick@wg0', result.stdout)
        self.assertNotIn('verify done', result.stdout)

    def test_healthy_roles_and_disabled_youtube(self):
        for role, youtube, fault in [('ru', 'off', 'missing-yt'), ('ru', 'zapret', ''), ('exit', 'off', '')]:
            with self.subTest(role=role, youtube=youtube):
                result = self.run_verify(fault, role, youtube)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn('verify done', result.stdout)

    def test_individual_entry_failures(self):
        faults = {
            'forwarding': 'ip_forward', 'firewall': 'ufw',
            'fail2ban': 'fail2ban', 'node_exporter': 'node_exporter', 'sing-box': 'sing-box',
            'wg-quick@wg0': 'wg-quick@wg0', 'openvpn-server@server': 'openvpn-server@server',
            'strongswan-starter': 'strongswan-starter', 'anysda-ikev2-routing': 'anysda-ikev2-routing',
            'anysda-tproxy-watchdog.timer': 'tproxy wd', 'anysda-ikev2-sync.timer': 'ikev2 sync',
            'tproxy-rule': 'tproxy:', 'tproxy-route': 'tproxy:', 'port-7898': 'tproxy udp',
            'networkd': 'networkd', 'ikev2-conn': 'ikev2:', 'swanctl-error': 'swanctl',
            'port-500': 'udp/500', 'port-4500': 'udp/4500', 'xfrm': 'xfrm0',
            'missing-status': 'ikev2 sync', 'bad-status': 'ikev2 sync',
            'stale-status': 'ikev2 sync', 'future-status': 'ikev2 sync',
            'empty-targets': 'mgmt scrape', 'missing-target': 'mgmt scrape',
            'down-target': 'mgmt scrape', 'bad-json': 'mgmt scrape',
            'http://127.0.0.1:8428/api/v1/targets': 'mgmt scrape',
            'http://127.0.0.1:80/': 'frontend', 'http://127.0.0.1:8428/-/healthy': 'vmsingle',
            'ssh-error': 'ssh exit', 'ss-error': 'tproxy udp', 'ip-error': 'tproxy:',
            'sysctl-error': 'ip_forward', 'ufw-error': 'ufw',
            'sysctl-partial': 'ip_forward', 'ufw-partial': 'ufw',
            'ss-partial': 'tproxy udp', 'ip-partial': 'tproxy:',
            'swanctl-partial': 'swanctl', 'curl-partial': 'mgmt scrape',
        }
        for fault, label in faults.items():
            with self.subTest(fault=fault):
                result = self.run_verify(fault)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertTrue(any('✗ ' in line and label in line for line in result.stdout.splitlines()), result.stdout)
                self.assertIn('verify FAILED:', result.stdout)
                self.assertNotIn('verify done', result.stdout)

    def test_enabled_youtube_failures(self):
        for fault, check in (('missing-yt', 'yt-zapret'), ('yt-failed', 'yt-zapret'),
                             ('yt-failover', 'yt-failover')):
            with self.subTest(fault=fault):
                result = self.run_verify(fault, youtube='zapret')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(f'✗ {check}:', result.stdout)

    def test_individual_exit_failures(self):
        for fault in ('port-443', 'port-8443', 'port-9443', 'missing-mgmt-port', 'sing-box', 'ss-error'):
            with self.subTest(fault=fault):
                result = self.run_verify(fault, role='exit')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('verify FAILED:', result.stdout)


if __name__ == '__main__':
    unittest.main()
