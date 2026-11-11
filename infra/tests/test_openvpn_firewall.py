import unittest

from test_ufw_reconcile import FAKE_UFW
import vpn_roles


class OpenvpnFirewallTests(unittest.TestCase):
    def test_server_transport_aliases(self):
        protocols = vpn_roles.package('entryvpn').ovpn_protocols
        for proto in ('udp', 'udp4', 'udp6', 'tcp', 'tcp4', 'tcp6',
                      'tcp-server', 'tcp4-server', 'tcp6-server'):
            with self.subTest(proto=proto):
                expected = proto + '-server' if proto in ('tcp', 'tcp4', 'tcp6') else proto
                self.assertEqual(protocols(proto), (expected, proto[:3]))
        with self.assertRaises(ValueError):
            protocols('sctp')

    def test_transport_variants_open_listener_port(self):
        # ufw получает первые три буквы OVPN_PROTO: тот же протокол, что отдаёт ovpn_protocols.
        self.assertIn('openvpn_ufw_proto: "{{ openvpn_proto[:3] }}"', vpn_roles.role_defaults('openvpn'))
        for transport in ('udp', 'tcp'):
            with self.subTest(transport=transport):
                values = {'openvpn_port': '1194', 'openvpn_ufw_proto': transport, 'openvpn_dns': '10.99.0.1',
                          'openvpn_subnets': ['10.67.67.0/24', '10.67.68.0/24']}
                rules = [' '.join(rule) for rule in
                         vpn_roles.ufw_apply(vpn_roles.ufw_groups('openvpn', values), [], FAKE_UFW)]
                self.assertTrue(any(rule.startswith(f'allow 1194/{transport} comment') for rule in rules), rules)
                self.assertTrue(any(rule.startswith('allow proto udp from 10.67.68.0/24 to 10.99.0.1 port 53 ')
                                    for rule in rules), rules)


if __name__ == '__main__':
    unittest.main()
