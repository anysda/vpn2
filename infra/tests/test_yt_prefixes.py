import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_deploy import ROOT


class PrefixTests(unittest.TestCase):
    def generate(self, prefixes):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'goog.json'
            path.write_text(json.dumps({'prefixes': prefixes}))
            return subprocess.run([sys.executable, str(ROOT / 'infra/lib/gen-yt-quic-nft.py'), str(path)],
                                  capture_output=True, text=True, timeout=10)

    def test_rejects_incomplete_or_widened_list(self):
        v4 = {'ipv4Prefix': '192.0.2.0/24'}
        v6 = {'ipv6Prefix': '2001:db8::/32'}
        for prefixes in ([v4], [v6], [v4, v6, {}],
                         [v4, v6, {'ipv4Prefix': '0.0.0.0/0'}],
                         [v4, v6, {'ipv4Prefix': '198.51.100.1/24'}],
                         [{'ipv4Prefix': '192.0.2.0/24', 'ipv6Prefix': '2001:db8::/32'}]):
            with self.subTest(prefixes=prefixes):
                result = self.generate(prefixes)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, '')

    def test_collapses_overlaps_and_covers_all_tunnels(self):
        result = self.generate([{'ipv4Prefix': '192.0.2.0/24'},
                                {'ipv4Prefix': '192.0.2.0/25'},
                                {'ipv6Prefix': '2001:db8::/32'}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('192.0.2.0/25', result.stdout)
        for iface in ('wg0', 'tun0', 'xfrm0'):
            for family in ('ip daddr @google_v4', 'ip6 daddr @google_v6'):
                self.assertIn(f'iifname "{iface}" udp dport 443 {family} counter drop', result.stdout)


if __name__ == '__main__':
    unittest.main()
