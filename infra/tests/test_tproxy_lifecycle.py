import os
import subprocess
import sys
import unittest

from router_support import ROOT, SB


@unittest.skipUnless(os.environ.get('RUN_NETNS_TESTS') == '1',
                     'set RUN_NETNS_TESTS=1 with sudo/iproute2/iptables available')
class TproxyLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
    def test_exit_failure_does_not_fall_back_to_entry_for_tcp_or_udp(self):
        result = subprocess.run([
            'sudo', '-n', 'unshare', '--net', 'env', f'SING_BOX_BIN={SB}',
            sys.executable, str(ROOT / 'infra/tests/probe_exit_failure.py'),
        ], capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('healthy exit: HTTP 200', result.stdout)
        self.assertIn('healthy exit: UDP echo', result.stdout)
        self.assertIn('exit down: request failed; target_hits=1', result.stdout)
        self.assertIn('exit down: UDP failed; target_hits=1', result.stdout)

    def test_wireguard_stop_preserves_openvpn_routing(self):
        functions = []
        for stage, name in [('28-wireguard', 'wg'), ('29-openvpn', 'ovpn')]:
            source = (ROOT / f'infra/scripts/{stage}.sh').read_text()
            routing = source.split("<<'IPTSEOF'\n", 1)[1].split('\nIPTSEOF', 1)[0]
            functions.append(f'{name}() {{ bash -s -- "$1" <<\'ROUTING\'\n{routing}\nROUTING\n}}')
        shell = '\n'.join(functions) + '''
set -euo pipefail
export WG_SUBNETS='10.66.66.0/24'
ip link set lo up
ip link add wg0 type dummy
ip link add tun0 type dummy
ip link set wg0 up
ip link set tun0 up
wg up
ovpn up
before=$(iptables -t mangle -S)
wg up
ovpn up
test "$before" = "$(iptables -t mangle -S)"
wg down
ip rule show
iptables -S INPUT
iptables -t mangle -C PREROUTING -i tun0 -j ANYSDA_OVPN_TPROXY
ip rule show | grep 'fwmark 0x42 lookup 101'
iptables -C INPUT -m mark --mark 0x42/0x42 -j ACCEPT
ip route show table 101 | grep 'local default'
echo 'WG stopped; OpenVPN TPROXY policy route and INPUT acceptance preserved'
'''
        result = subprocess.run(['sudo', '-n', 'unshare', '--net', 'bash', '-s'],
                                input=shell, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
