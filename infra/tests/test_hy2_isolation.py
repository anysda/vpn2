"""Regression through generated hub/exit configs on the unmodified binary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from router_support import ROOT, SB, generate


class Hy2IsolationTests(unittest.TestCase):
    def test_every_data_exit_uses_independent_stream_windows(self):
        config = json.loads(generate())
        outbounds = {out['tag']: out for out in config['outbounds']}
        for node in ('aa', 'bb'):
            for kind, port in (('direct', 10443), ('warp', 10444)):
                with self.subTest(node=node, kind=kind):
                    tag = f'hy2-{node}-{kind}'
                    inner = outbounds[tag]
                    self.assertEqual(inner['type'], 'trojan')
                    self.assertEqual(inner['multiplex'], {
                        'enabled': True, 'protocol': 'h2mux', 'max_connections': 4,
                    })
                    outer = outbounds[inner['detour']]
                    self.assertEqual(outer['type'], 'hysteria2')
                    self.assertEqual(outer['obfs']['type'], 'salamander')
                    self.assertEqual(inner['password'], outer['password'])
                    self.assertEqual((inner['server'], inner['server_port']), ('127.0.0.1', port))
                    self.assertEqual(outbounds[f'pin-{tag}']['outbounds'], [tag, 'foreign-best'])
            self.assertEqual(outbounds[f'hy2-{node}-mgmt']['type'], 'hysteria2')

    @unittest.skipUnless(SB and os.environ.get('RUN_NETNS_TESTS') == '1',
                         'set SING_BOX_BIN and RUN_NETNS_TESTS=1 with sudo/iproute2')
    def test_frozen_readers_cannot_starve_generated_exit(self):
        for variant, kind in (('generated', 'direct'), ('generated-warp', 'warp')):
            with self.subTest(kind=kind):
                result = subprocess.run([
                    'sudo', '-n', 'unshare', '--net', 'sh', '-ec',
                    'ip link set lo up; ip address add 192.0.2.20/32 dev lo; exec "$@"',
                    'probe', 'env', f'INFRA_TEST_ROOT={ROOT}', sys.executable,
                    str(Path(__file__).with_name('probe_hy2_isolation.py')),
                    '--router-binary', SB, '--server-binary', SB,
                    '--variant', variant, '--origin-address', '192.0.2.20',
                ], text=True, capture_output=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output['frozen']['bytes'], 10 * 1024 * 1024)
                self.assertIn('delay', output['delay'])
                self.assertEqual(output['udp']['bytes'], 1152)
                self.assertIn([f'hy2-aa-{kind}', f'pin-hy2-aa-{kind}'], output['chains'])


if __name__ == '__main__':
    unittest.main()
