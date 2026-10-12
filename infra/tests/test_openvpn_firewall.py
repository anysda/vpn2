import tempfile
import unittest

from test_deploy import bash, source


class OpenvpnFirewallTests(unittest.TestCase):
    def test_transport_variants_open_listener_port(self):
        block = source('infra/scripts/29-openvpn.sh').split('mkdir -p /run/openvpn-server\n', 1)[1].split('# ── 5.', 1)[0]
        for proto, transport in (('udp', 'udp'), ('udp4', 'udp'), ('udp6', 'udp'),
                                 ('tcp-server', 'tcp'), ('tcp4-server', 'tcp'), ('tcp6-server', 'tcp')):
            with self.subTest(proto=proto), tempfile.TemporaryDirectory() as directory:
                result = bash(f'OVPN_PROTO={proto}\n' + '''
OVPN_PORT=1194
OVPN_SUBNETS='10.67.67.0/24 10.67.68.0/24'
sysctl() { :; }
ufw() { echo "$*" >> calls; }
''' + block + '\ncat calls\n', directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'allow 1194/{transport} comment', result.stdout)
                self.assertIn('allow proto udp from 10.67.68.0/24 to 10.99.0.1 port 53', result.stdout)


if __name__ == '__main__':
    unittest.main()
