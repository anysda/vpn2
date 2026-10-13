"""Ping получает честный отказ на entry для всех TPROXY VPN-входов."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from test_deploy import bash, source


PROTOCOLS = (('28-wireguard', 'wg0'), ('29-openvpn', 'tun0'), ('27-ikev2', 'xfrm0'))


def routing_script(stage, directory):
    text = source(f'infra/scripts/{stage}.sh')
    if stage == '27-ikev2':
        template = text.split('<<IPTSEOF\n', 1)[1].split('\nIPTSEOF', 1)[0]
        rendered = bash('''
WAN_IF=wan
IKEV2_SUBNETS=10.68.68.0/24
IKEV2_SUBNET6=fd68:68::/112
cat <<IPTSEOF
''' + template + '\nIPTSEOF\n', directory)
        if rendered.returncode:
            raise RuntimeError(rendered.stderr)
        text = rendered.stdout
    else:
        text = text.split("<<'IPTSEOF'\n", 1)[1].split('\nIPTSEOF', 1)[0]
    return text.replace('/run/anysda-vpn-routing.lock', str(Path(directory) / 'routing.lock'))


class IcmpRoutingTests(unittest.TestCase):
    def test_every_protocol_rejects_only_forwarded_echo_and_removes_own_rule(self):
        for stage, iface in PROTOCOLS:
            with self.subTest(protocol=stage), tempfile.TemporaryDirectory() as directory:
                script = routing_script(stage, directory)
                for action, operation in (('up', '-I FORWARD 1'), ('down', '-D FORWARD')):
                    result = bash('''
ip() { echo 'local default dev lo'; }
iptables() { [[ " $* " != *" -C "* ]] && echo "iptables $*"; }
ip6tables() { [[ " $* " != *" -C "* ]]; }
export WG_SUBNETS=10.66.66.0/24
''' + f'set -- {action}\n' + script, directory)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    rejects = [line for line in result.stdout.splitlines()
                               if ' -j REJECT ' in line]
                    self.assertEqual(rejects, [
                        f'iptables {operation} -i {iface} -p icmp --icmp-type echo-request '
                        '-m comment --comment vpn2: Hysteria2 has no ICMP '
                        '-j REJECT --reject-with icmp-proto-unreachable',
                    ])

    @unittest.skipUnless(os.environ.get('RUN_NETNS_TESTS') == '1',
                         'set RUN_NETNS_TESTS=1 with sudo/iproute2/iptables/ping available')
    def test_ping_reject_precedes_accept_is_idempotent_and_preserves_local_ping(self):
        for stage, iface in PROTOCOLS:
            with self.subTest(protocol=stage), tempfile.TemporaryDirectory() as directory:
                script = Path(directory) / 'routing.sh'
                script.write_text(routing_script(stage, directory))
                shell = '''
set -euo pipefail
ns=vpn2-icmp-$$
trap 'ip netns del "$ns"' EXIT
ip netns add "$ns"
ip link set lo up
ip link add wan type dummy
ip link set wan up
ip route add default dev wan
ip link add "$IFACE" type veth peer name peer
ip link set peer netns "$ns"
ip address add 192.0.2.1/24 dev "$IFACE"
ip link set "$IFACE" up
ip -n "$ns" link set lo up
ip -n "$ns" address add 192.0.2.2/24 dev peer
ip -n "$ns" link set peer up
ip -n "$ns" route add default via 192.0.2.1
sysctl -qw net.ipv4.ip_forward=1
# Даже существующее разрешение ICMP в ufw не должно выпускать ping с entry.
iptables -A FORWARD -p icmp -j ACCEPT
export WG_SUBNETS=10.66.66.0/24
bash "$ROUTING" up
before=$(iptables -S FORWARD)
bash "$ROUTING" up
test "$before" = "$(iptables -S FORWARD)"
ip netns exec "$ns" ping -n -c 1 -W 1 192.0.2.1
set +e
out=$(ip netns exec "$ns" ping -n -c 3 -W 1 198.51.100.1 2>&1)
status=$?
set -e
printf '%s\n' "$out"
test "$status" = 1
[[ "$out" == *'Protocol Unreachable'* ]]
# ICMP-ошибки не матчятся правилом echo-request, TCP/UDP по-прежнему TPROXY.
iptables -t mangle -S | grep -- '-p tcp .*TPROXY'
iptables -t mangle -S | grep -- '-p udp .*TPROXY'
bash "$ROUTING" down
iptables -C FORWARD -p icmp -j ACCEPT
test "$(iptables -S FORWARD | wc -l)" = 2
'''
                shell = f'IFACE={iface}\nROUTING={shlex.quote(str(script))}\n' + shell
                result = subprocess.run(['sudo', '-n', 'unshare', '--net', 'bash', '-s'],
                                        input=shell, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
