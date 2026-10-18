"""Клиент VPN не достаёт link-local, частные сети и службы самого entry."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from router_support import SB, apply, prepare
from test_router_telegram import ENV, GENERATOR, route

ENTRY = '203.0.113.10 2001:db8::10 172.17.0.1'


def generate(mode):
    return json.loads(subprocess.check_output(
        [sys.executable, GENERATOR],
        env={**ENV, 'YT_ROUTE': mode, 'YT_QUIC': 'block', 'ENTRY_ADDRS': ENTRY}, text=True))


class ClientBlockTests(unittest.TestCase):
    def test_metadata_link_local_and_entry_services_are_rejected(self):
        for mode in ('off', 'direct', 'zapret'):
            cfg = generate(mode)
            for ip, port in (('169.254.169.254', 80), ('169.254.0.1', 443), ('fe80::1', 80),
                             ('172.17.0.1', 80), ('100.100.100.200', 80), ('10.1.2.3', 443),
                             ('203.0.113.10', 8080), ('203.0.113.10', 9090),
                             ('2001:db8::10', 3000)):
                for network in ('tcp', 'udp'):
                    for extra in ({}, {'geoip': ['ru']}, {'domain': 'metadata.example.ru'}):
                        with self.subTest(mode=mode, ip=ip, port=port, network=network, **extra):
                            self.assertEqual(route(cfg, ip=ip, port=port, network=network,
                                                   **extra), 'reject')
            # Публичные порты entry (SSH, панель) клиенту доступны как снаружи.
            for port in (22, 80, 443):
                self.assertNotEqual(route(cfg, ip='203.0.113.10', port=port), 'reject')
            self.assertEqual(route(cfg, ip='203.0.113.10', port=443, network='udp'), 'reject')

    def test_entry_service_traffic_is_not_broken(self):
        cfg = generate('zapret')
        self.assertEqual(route(cfg, ip='203.0.113.1'), 'lane-07')
        self.assertEqual(route(cfg, ip='203.0.113.1', geoip=['ru']), 'direct-ru')
        self.assertEqual(route(cfg, inbound='mon-aa', ip='127.0.0.1'), 'hy2-aa-mgmt')
        self.assertEqual(route(cfg, inbound='tg-proxy'), 'tg-best')
        dns = route(cfg, ip='10.99.0.1', port=53, network='udp', protocol='dns')
        self.assertEqual(dns, 'dns-unfiltered')

    def test_block_is_ahead_of_every_exit_choice(self):
        cfg = generate('zapret')
        rules = cfg['route']['rules']
        block = next(i for i, rule in enumerate(rules)
                     if rule.get('action') == 'reject' and '169.254.0.0/16' in rule.get('ip_cidr', []))
        self.assertTrue(all('outbound' not in rule or 'inbound' in rule for rule in rules[:block]),
                        'клиентский выход выбирается раньше блокировки')
        # Ручные маршруты панели встают после «головы»: inbound- и reject-правил.
        self.assertTrue(all('inbound' in rule or rule.get('action') in ('reject', 'sniff')
                            for rule in rules[:block + 1]))

    @unittest.skipUnless(SB, 'set SING_BOX_BIN to sing-box 1.12.25')
    def test_manual_default_route_stays_below_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            script = prepare(directory, ENTRY_ADDRS=ENTRY)
            (directory / 'manual.json').write_text(json.dumps([
                {'type': 'ip_cidr', 'value': '0.0.0.0/0', 'outbound': 'hy2-aa-direct'}]))
            result = apply(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            rules = json.loads((directory / 'config.json').read_text())['route']['rules']
            manual = next(i for i, rule in enumerate(rules)
                          if any(tag.startswith('manual-') for tag in rule.get('rule_set', [])))
            for net in ('169.254.0.0/16', 'fe80::/10'):
                block = next(i for i, rule in enumerate(rules)
                             if rule.get('action') == 'reject' and net in rule.get('ip_cidr', []))
                self.assertLess(block, manual, net)
            entry = next(i for i, rule in enumerate(rules)
                         if rule.get('action') == 'reject' and rule.get('type') == 'logical'
                         and '203.0.113.10/32' in rule['rules'][0].get('ip_cidr', []))
            self.assertLess(entry, manual)


if __name__ == '__main__':
    unittest.main()
