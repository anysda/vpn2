"""Зарубежный трафик клиента WireGuard идёт по его дорожке, а не по foreign-best.

docs/routing.md: клиентский VPN-IP → lane-NN, остальное → foreign-best.
Переключение foreign-best не меняет выход клиента туннеля: его меняет
сторож через селектор дорожки.
"""

import ipaddress
import json
from pathlib import Path
import unittest

from router_support import ROOT, generate

WG_SUBNETS = json.loads((Path(ROOT) / 'app/shared/vpn-pools.json').read_text())['wg']['subnets']


def matches_source(rule, ip):
    return any(ip in ipaddress.ip_network(net) for net in rule.get('source_ip_cidr', []))


class WgClientLaneRouteTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(generate())
        self.rules = self.cfg['route']['rules']
        self.outbounds = {o['tag']: o for o in self.cfg['outbounds']}

    def test_tproxy_inbound_sniffs_before_any_route(self):
        first = self.rules[0]
        self.assertEqual(first, {'inbound': ['wg-tproxy-in'], 'action': 'sniff'})

    def test_every_wg_client_lands_on_its_lane_with_warp_legs(self):
        warp = {t for t in self.outbounds if t.startswith('hy2-') and t.endswith('-warp')}
        self.assertTrue(warp)
        for subnet in WG_SUBNETS:
            for host in ipaddress.ip_network(subnet).hosts():
                with self.subTest(ip=str(host)):
                    hit = [r for r in self.rules if matches_source(r, host)]
                    self.assertEqual(len(hit), 1, hit)
                    lane = hit[0]['outbound']
                    self.assertEqual(lane, f'lane-{int(host) % 256 % 4:02d}')
                    selector = self.outbounds[lane]
                    self.assertEqual(selector['type'], 'selector')
                    self.assertLessEqual(warp, set(selector['outbounds']))
                    self.assertNotIn('foreign-best', selector['outbounds'])

    def test_no_rule_sends_wg_source_to_foreign_best(self):
        for rule in self.rules:
            if rule.get('outbound') == 'foreign-best':
                self.assertNotIn('source_ip_cidr', rule)
                self.assertIn('inbound', rule)
                self.assertNotIn('wg-tproxy-in', rule['inbound'])

    def test_cloudflare_is_not_domestic(self):
        text = json.dumps(self.rules)
        self.assertNotIn('cloudflare', text)
        lane = next(i for i, r in enumerate(self.rules)
                    if str(r.get('outbound', '')).startswith('lane-'))
        domains = [r for r in self.rules[:lane] if 'domain_suffix' in json.dumps(r)]
        for rule in domains:
            for suffix in json.dumps(rule).split('"'):
                self.assertFalse('cloudflare.com'.endswith(suffix) and suffix.startswith('.'),
                                 rule)


if __name__ == '__main__':
    unittest.main()
