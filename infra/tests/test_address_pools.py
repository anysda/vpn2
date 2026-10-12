import ipaddress
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from test_deploy import ROOT, bash, source


class AddressPoolTests(unittest.TestCase):
    def test_capacity_order_and_no_overlap_with_project_networks(self):
        pools = json.loads(source('app/shared/vpn-pools.json'))
        self.assertEqual(pools['capacity'], 2000)
        networks = []
        # Служебный lo/бывший mesh, WARP fallback, Docker и адрес WARP из wgcf.
        reserved = list(map(ipaddress.ip_network, [
            '10.99.0.0/24', '10.111.111.0/24', '172.16.0.0/12',
            '192.168.0.0/16', '100.64.0.0/10', '127.0.0.0/8',
        ]))
        for key, octet, last in [('wg', 66, 254), ('ovpn', 67, 239), ('ikev2', 68, 254)]:
            pool = pools[key]
            self.assertEqual(pool['subnets'][0], f'10.{octet}.{octet}.0/24')
            self.assertEqual(pool['legacyLast'], last)
            addresses = []
            for index, cidr in enumerate(pool['subnets']):
                net = ipaddress.ip_network(cidr)
                self.assertEqual(net.prefixlen, 24)
                for other in networks + reserved:
                    self.assertFalse(net.overlaps(other), f'{net} overlaps {other}')
                networks.append(net)
                addresses.extend(net.network_address + i for i in range(2, (last if index == 0 else 254) + 1))
            self.assertGreaterEqual(len(addresses), 2000)
            addresses = addresses[:pools['capacity']]
            self.assertEqual(len(set(addresses)), 2000)
            self.assertEqual(str(addresses[last - 1]), f'10.{octet}.64.2')
            if key == 'ovpn':
                self.assertTrue(all(ipaddress.ip_address(f'10.67.67.{i}') not in addresses for i in range(240, 254)))

        # Любой новый private IPv4 literal в исполняемом коде требует включить
        # его сеть в эту проверку. Широкие RFC1918 bypass-маршруты не пулы.
        known = networks + reserved + list(map(ipaddress.ip_network, ['10.0.0.0/32', '192.0.0.0/32']))
        files = [p for directory in ('infra', 'app/server') for p in (ROOT / directory).rglob('*')
                 if p.suffix in ('.py', '.sh', '.ts', '.tpl') and 'tests' not in p.parts]
        for path in files:
            for literal in re.findall(r'(?<![\d.])(?:10|172|192)\.\d+\.\d+\.\d+(?![\d.])', path.read_text()):
                address = ipaddress.ip_address(literal)
                if address.is_private and not address.is_loopback and not address.is_reserved:
                    # RFC5737 — примеры, не выделяемые проектом сети.
                    if address in ipaddress.ip_network('192.0.2.0/24'):
                        continue
                    self.assertTrue(any(address in net for net in known), f'unclassified pool/address {literal} in {path}')

    def test_deployed_manifest_and_shell_match_panel(self):
        pools = json.loads(source('app/shared/vpn-pools.json'))
        with tempfile.TemporaryDirectory() as directory:
            result = bash(f'source "{ROOT}/infra/lib/anysda-svc.sh"\nvpn_pool_subnets wg ovpn ikev2', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.split(), [n for p in ('wg', 'ovpn', 'ikev2') for n in pools[p]['subnets']])
        self.assertIn('push "$DEPLOY_ROOT/../app/shared/vpn-pools.json" || return', source('infra/deploy.sh'))

    def test_wireguard_routes_on_start(self):
        stage = source('infra/scripts/28-wireguard.sh')
        routing = stage.split("<<'IPTSEOF'\n", 1)[1].split('\nIPTSEOF', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            routing = routing.replace('/run/anysda-vpn-routing.lock', directory + '/routing.lock')
            result = bash(f'source "{ROOT}/infra/lib/anysda-svc.sh"\n' + '''
WG_SUBNETS=$(vpn_pool_subnets wg)
ip() { echo "ip $*"; }
iptables() { [[ " $* " != *" -C "* ]] && echo "iptables $*"; }
ip6tables() { [[ " $* " != *" -C "* ]] && echo "ip6tables $*"; }
set -- up
''' + routing, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            for cidr in json.loads(source('app/shared/vpn-pools.json'))['wg']['subnets']:
                self.assertIn(f'ip route replace {cidr} dev wg0', result.stdout)

    def test_ikev2_routes_and_openvpn_extra_routes(self):
        pools = json.loads(source('app/shared/vpn-pools.json'))
        stage = source('infra/scripts/27-ikev2.sh')
        template = stage.split('cat > /usr/local/sbin/anysda-ikev2-routing.sh <<IPTSEOF\n', 1)[1].split('\nIPTSEOF', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            template = template.replace('/run/anysda-vpn-routing.lock', directory + '/routing.lock')
            script = f'source "{ROOT}/infra/lib/anysda-svc.sh"\n' + '''
IKEV2_SUBNETS=$(vpn_pool_subnets ikev2)
IKEV2_SUBNET=10.68.68.0/24
IKEV2_SUBNET6=fd68:68::/112
WAN_IF=eth0
cat > routing.sh <<IPTSEOF
''' + template + '''
IPTSEOF
ip() { echo "ip $*"; }
iptables() { [[ " $* " != *" -C "* ]] && echo "iptables $*"; }
ip6tables() { [[ " $* " != *" -C "* ]] && echo "ip6tables $*"; }
source routing.sh up
'''
            result = bash(script, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            for cidr in pools['ikev2']['subnets']:
                self.assertIn(f'ip route replace {cidr} dev xfrm0', result.stdout)
            ovpn = source('infra/scripts/29-openvpn.sh')
            loop = 'for subnet in $OVPN_SUBNETS;' + ovpn.split('for subnet in $OVPN_SUBNETS;', 1)[1].split('\ndone', 1)[0] + '\ndone'
            result = bash(f'source "{ROOT}/infra/lib/anysda-svc.sh"\n' + '''
OVPN_SUBNETS=$(vpn_pool_subnets ovpn)
OVPN_SUBNET=10.67.67.0
OVPN_DIR=$PWD
''' + loop, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            routes = (Path(directory) / 'server.conf').read_text().splitlines()
            self.assertEqual(routes, [f'route {cidr[:-3]} 255.255.255.0' for cidr in pools['ovpn']['subnets'][1:]])

    def test_dns_firewall_and_adguard_cover_every_subnet(self):
        pools = json.loads(source('app/shared/vpn-pools.json'))
        for key, filename, variable in [('wg', '28-wireguard', 'WG_SUBNETS'), ('ovpn', '29-openvpn', 'OVPN_SUBNETS'), ('ikev2', '27-ikev2', 'IKEV2_SUBNETS')]:
            stage = source(f'infra/scripts/{filename}.sh')
            start = stage.rfind(f'for subnet in ${variable};')
            loop = stage[start:].split('\ndone', 1)[0] + '\ndone'
            with tempfile.TemporaryDirectory() as directory:
                result = bash(f'source "{ROOT}/infra/lib/anysda-svc.sh"\n{variable}=$(vpn_pool_subnets {key})\n' + '''
IKEV2_DNS=10.99.0.1
ufw() { echo "$*" >> rules; }
''' + loop, directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                rules = (Path(directory) / 'rules').read_text()
                for cidr in pools[key]['subnets']:
                    for protocol in ('tcp', 'udp'):
                        self.assertIn(f'allow proto {protocol} from {cidr} to 10.99.0.1 port 53', rules)
        stage = source('infra/scripts/22-adguard.sh')
        function = 'patch_agh_config() {' + stage.split('patch_agh_config() {', 1)[1].split('\n}', 1)[0] + '\n}'
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'AdGuardHome.yaml'
            config.write_text('dns:\n  aaaa_disabled: true\n  ratelimit_whitelist: [203.0.113.1]\n')
            result = bash(f'source "{ROOT}/infra/lib/anysda-svc.sh"\nHOST_TAG=ru\nsvc_forget() {{ :; }}\n' + function + '\npatch_agh_config AdGuardHome.yaml\npatch_agh_config AdGuardHome.yaml', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            import yaml
            addresses = yaml.safe_load(config.read_text())['dns']['ratelimit_whitelist']
            self.assertEqual(len(addresses), 24 * 254 + 1)
            self.assertEqual(len(set(addresses)), len(addresses))
            self.assertIn('203.0.113.1', addresses)

    def test_router_lanes_include_new_addresses_with_old_env(self):
        import os
        env = dict(os.environ, EXIT_TAGS='se', HY2_DIRECT_PORT='443', HY2_WARP_PORT='444', HY2_MGMT_PORT='445',
                   WG_OUT_IFACE='eth0', RU_CLASH_SECRET='test', DOMAIN_SE='exit.example.test',
                   SE_DIRECT='test', SE_WARP='test', SE_MGMT='test', SE_OBFS='test', MGMT_IP_SE='10.99.0.2',
                   LANE_SUBNETS='10.66.66.0/24 10.67.67.0/24 10.68.68.0/24')
        result = subprocess.run(['python3', str(ROOT / 'infra/lib/gen-router-config.py')], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        routed = {cidr for rule in config['route']['rules'] if rule.get('outbound', '').startswith('lane-') for cidr in rule['source_ip_cidr']}
        self.assertEqual(len(routed), 24 * 254)
        for second in (66, 67, 68):
            for third in range(64, 72):
                self.assertIn(f'10.{second}.{third}.2/32', routed)


if __name__ == '__main__':
    unittest.main()
