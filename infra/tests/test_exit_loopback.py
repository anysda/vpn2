import os
from pathlib import Path
import subprocess
import sys
import unittest

from router_support import ROOT, SB


@unittest.skipUnless(SB and os.environ.get('RUN_NETNS_TESTS') == '1',
                     'set SING_BOX_BIN and RUN_NETNS_TESTS=1 with sudo/iproute2')
class ExitLoopbackTests(unittest.TestCase):
    def test_user_tunnel_cannot_reach_exit_loopback(self):
        result = subprocess.run([
            'sudo', '-n', 'unshare', '--net', 'env', f'SING_BOX_BIN={SB}',
            f'INFRA_TEST_ROOT={ROOT}', sys.executable,
            str(Path(__file__).with_name('probe_exit_loopback.py')),
        ], text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('direct: loopback blocked; public TCP/UDP reachable', result.stdout)
        self.assertIn('mgmt: ports 22/9100 reachable; other destinations blocked', result.stdout)


if __name__ == '__main__':
    unittest.main()
