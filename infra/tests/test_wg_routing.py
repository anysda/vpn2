import tempfile
import unittest

from test_deploy import bash, source


class RoutingTests(unittest.TestCase):
    def test_wireguard_down_preserves_shared_tproxy(self):
        stage = source('infra/scripts/28-wireguard.sh')
        routing = stage.split("<<'IPTSEOF'\n", 1)[1].split('\nIPTSEOF', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            result = bash('''
ip() { echo "ip $*"; }
iptables() { echo "iptables $*"; }
ip6tables() { echo "ip6tables $*"; }
set -- down
''' + routing, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('ip rule del', result.stdout)
            self.assertNotIn('iptables -D INPUT', result.stdout)
            self.assertIn('-D PREROUTING -i wg0', result.stdout)


if __name__ == '__main__':
    unittest.main()
