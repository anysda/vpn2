import tempfile
import unittest

from test_deploy import bash
from vpn_roles import routing as role_routing


class RoutingTests(unittest.TestCase):
    def test_wireguard_down_preserves_shared_tproxy(self):
        routing = role_routing('28-wireguard')
        with tempfile.TemporaryDirectory() as directory:
            routing = routing.replace('/run/anysda-vpn-routing.lock', directory + '/routing.lock')
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
