"""Emergency Hysteria2 for real: sing-box, nftables and the package CLI in namespaces."""

import os
from pathlib import Path
import subprocess
import sys
import unittest

from router_support import ROOT, SB


class EmergencyHy2Tests(unittest.TestCase):
    @unittest.skipUnless(SB and os.environ.get('RUN_NETNS_TESTS') == '1',
                         'set SING_BOX_BIN and RUN_NETNS_TESTS=1 with sudo/iproute2/nftables')
    def test_real_isolation_revocation_and_restart(self):
        result = subprocess.run([
            'sudo', '-n', 'unshare', '--net', '--mount', 'env', f'INFRA_TEST_ROOT={ROOT}',
            f'SING_BOX_BIN={SB}', sys.executable,
            str(Path(__file__).with_name('probe_emergency_hy2.py')),
        ], capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for line in ('isolation: public TCP/UDP pass', 'sessions: connected -> seen',
                     'old credentials rejected after restart; new credentials work'):
            self.assertIn(line, result.stdout)
        print(result.stdout, end='')


if __name__ == '__main__':
    unittest.main(verbosity=2)
