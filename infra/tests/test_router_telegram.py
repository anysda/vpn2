#!/usr/bin/env python3
"""Telegram must reach client exit selection even with a misleading sniffed name.

Run against an older generator with ROUTER_GENERATOR=/path/to/gen-router-config.py.
The matcher below covers only the generated rule fields used in these fixtures;
unknown fields fail explicitly. It does not emulate sniffing or network transport.
"""

import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from router_support import exit_cert


# sing-box ip_is_private: адрес не публичный (RFC 1918, ULA, loopback, link-local).
PRIVATE_NETS = [ipaddress.ip_network(net) for net in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8',
    '169.254.0.0/16', 'fc00::/7', '::1/128', 'fe80::/10')]

GENERATOR = os.environ.get(
    'ROUTER_GENERATOR', str(Path(__file__).resolve().parents[1] / 'lib/gen-router-config.py'))
ENV = {
    'EXIT_TAGS': 'aa bb', 'HY2_DIRECT_PORT': '8443', 'HY2_WARP_PORT': '8444',
    'HY2_MGMT_PORT': '8445', 'WG_OUT_IFACE': 'lo', 'RU_CLASH_SECRET': 'test-only',
    'MGMT_IP': '192.0.2.1', 'LANE_SUBNETS': '192.0.2.0/24', 'LANES': '32',
    **{f'{tag}_{key}': 'test-only' for tag in ('AA', 'BB')
       for key in ('DIRECT', 'WARP', 'MGMT', 'OBFS')},
    'DOMAIN_AA': 'aa.example.test', 'DOMAIN_BB': 'bb.example.test',
    'MGMT_IP_AA': '192.0.2.2', 'MGMT_IP_BB': '192.0.2.3',
    'AA_HY2_CERT_PATH': exit_cert(), 'BB_HY2_CERT_PATH': exit_cert(),
}


def generate(mode, quic='block'):
    return json.loads(subprocess.check_output(
        [sys.executable, GENERATOR],
        env={**ENV, 'YT_ROUTE': mode, 'YT_QUIC': quic}, text=True))


def matches(rule, flow):
    checks = []
    for key, value in rule.items():
        if key in ('action', 'outbound', 'invert', 'type', 'mode',
                   'override_address', 'override_port'):
            continue
        if key == 'rules':
            children = [matches(child, flow) for child in value]
            checks.append(all(children) if rule['mode'] == 'and' else any(children))
        elif key in ('ip_cidr', 'source_ip_cidr'):
            addr = ipaddress.ip_address(flow['source' if key == 'source_ip_cidr' else 'ip'])
            checks.append(any(addr in ipaddress.ip_network(net) for net in value))
        elif key == 'domain_suffix':
            domain = flow.get('domain', '')
            checks.append(any(domain == suffix.lstrip('.') or domain.endswith(
                suffix if suffix.startswith('.') else '.' + suffix) for suffix in value))
        elif key == 'rule_set':
            labels = {f'{kind}-{label}' for kind in ('geoip', 'geosite')
                      for label in flow.get(kind, [])}
            checks.append(bool(set(value) & labels))
        elif key == 'ip_is_private':
            addr = ipaddress.ip_address(flow['ip'])
            checks.append(any(addr in net for net in PRIVATE_NETS) == value)
        elif key in ('inbound', 'network', 'port', 'protocol'):
            checks.append(flow.get(key) in (value if isinstance(value, list) else [value]))
        else:
            raise AssertionError(f'Unsupported fixture rule: {key}')
    result = all(checks)
    return not result if rule.get('invert') else result


def route(cfg, **changes):
    flow = {'source': '192.0.2.7', 'ip': '149.154.167.51', 'network': 'tcp',
            'port': 443, 'inbound': 'wg-tproxy-in', **changes}
    for rule in cfg['route']['rules']:
        # sniff только размечает соединение, подбор правил идёт дальше.
        if rule.get('action') == 'sniff' or not matches(rule, flow):
            continue
        return 'reject' if rule.get('action') == 'reject' else rule['outbound']
    return cfg['route']['final']


class TelegramRoutingTest(unittest.TestCase):
    def test_telegram_keeps_lane_despite_sniffed_domain_or_geo_database(self):
        # First and last addresses cover each published IPv4 Telegram prefix.
        prefixes = ('91.105.192.0/23', '91.108.4.0/22', '91.108.8.0/22',
                    '91.108.12.0/22', '91.108.16.0/22', '91.108.20.0/22',
                    '91.108.56.0/22', '149.154.160.0/20', '185.76.151.0/24')
        names = ('', 'cover.example.ru', 'cover.example.su', 'cover.example.рф',
                 'www.youtube.com', 'rr1.googlevideo.com')
        for mode in ('off', 'direct', 'zapret'):
            cfg = generate(mode)
            for prefix in prefixes:
                net = ipaddress.ip_network(prefix)
                for ip in (str(net.network_address), str(net.broadcast_address)):
                    for domain in names:
                        for network in ('tcp', 'udp'):
                            with self.subTest(mode=mode, ip=ip, domain=domain, network=network):
                                self.assertEqual(route(cfg, ip=ip, domain=domain,
                                                       network=network), 'lane-07')
            for labels in ({'geoip': ['ru']}, {'geosite': ['category-gov-ru']},
                           {'geosite': ['youtube']}):
                with self.subTest(mode=mode, labels=labels):
                    self.assertEqual(route(cfg, **labels), 'lane-07')

    def test_non_lane_telegram_uses_foreign_best(self):
        cfg = generate('zapret')
        self.assertEqual(route(cfg, source='198.51.100.7', domain='youtube.com'), 'foreign-best')

    def test_regular_routing_and_service_guards(self):
        for mode in ('off', 'direct', 'zapret'):
            cfg = generate(mode)
            for domain in ('example.ru', 'example.su', 'example.рф'):
                self.assertEqual(route(cfg, ip='203.0.113.1', domain=domain), 'direct-ru')
            for labels in ({'geoip': ['ru']}, {'geosite': ['category-gov-ru']}):
                self.assertEqual(route(cfg, ip='203.0.113.1', **labels), 'direct-ru')
            self.assertEqual(route(cfg, ip='10.1.2.3'), 'direct-ru')
            self.assertEqual(route(cfg, ip='203.0.113.1'), 'lane-07')
            self.assertEqual(route(cfg, inbound='tg-proxy'), 'tg-best')
            self.assertEqual(route(cfg, inbound='tg-proxy', ip='127.0.0.1'), 'reject')
            self.assertEqual(route(cfg, inbound='mon-aa', ip='127.0.0.1'), 'hy2-aa-mgmt')
            self.assertEqual(route(cfg, ip='127.0.0.1'), 'reject')
            self.assertEqual(route(cfg, ip='172.16.0.1', port=853), 'reject')
            self.assertEqual(route(cfg, ip='203.0.113.1', protocol='dns'), 'dns-unfiltered')
            for quic in ('block', 'allow'):
                cfg = generate(mode, quic)
                # zapret ведёт YouTube через selector с запасным выходом на VPN.
                target = {'off': 'lane-07', 'direct': 'youtube-ru'}.get(mode, 'youtube-auto')
                self.assertEqual(route(cfg, ip='203.0.113.1', domain='youtube.com'), target)
                self.assertEqual(route(cfg, ip='203.0.113.1', domain='youtube.com', network='udp'),
                                 'reject' if mode != 'off' and quic == 'block' else target)

    def test_manual_and_client_preference_insertion_points(self):
        # Same insertion contracts as stage 20's anysda-apply-routes: service
        # head, manual routes, automatic local rules, preferences, lanes.
        cfg = generate('zapret')
        rules = cfg['route']['rules']
        head = 0
        while 'inbound' in rules[head] or rules[head].get('action') == 'reject':
            head += 1
        rules.insert(head, {'ip_cidr': ['91.108.4.0/22'], 'outbound': 'pin-hy2-aa-direct'})
        at = next(i for i, r in enumerate(rules) if r.get('outbound', '').startswith('lane-'))
        rules.insert(at, {'source_ip_cidr': ['192.0.2.7/32'], 'outbound': 'pin-hy2-bb-direct'})
        self.assertEqual(route(cfg, ip='91.108.4.1', domain='youtube.com'), 'pin-hy2-aa-direct')
        self.assertEqual(route(cfg, domain='youtube.com'), 'pin-hy2-bb-direct')
        self.assertEqual(route(cfg, inbound='tg-proxy'), 'tg-best')
        self.assertEqual(route(cfg, ip='203.0.113.1', domain='youtube.com'), 'youtube-auto')

    def test_tproxy_preserves_destination_and_both_transports(self):
        cfg = generate('zapret')
        inbound = next(i for i in cfg['inbounds'] if i['tag'] == 'wg-tproxy-in')
        self.assertNotIn('network', inbound)
        self.assertEqual(cfg['route']['rules'][0], {'inbound': ['wg-tproxy-in'], 'action': 'sniff'})
        for outbound in cfg['outbounds']:
            if outbound['type'] in ('selector', 'urltest'):
                # Только YouTube рвёт соединения при уходе на VPN: иначе плеер
                # держит сломанный поток через обход.
                self.assertEqual(outbound['interrupt_exist_connections'],
                                 outbound['tag'] == 'youtube-auto')


if __name__ == '__main__':
    unittest.main()
