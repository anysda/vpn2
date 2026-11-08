#!/usr/bin/env python3
"""Внешняя проба 99-verify стучится в entry с адреса экзита, а не через WARP.

Ретранслятор пробы (verify-traffic.py, Relay) ходит через вход tg-proxy. Весь
этот вход уходил в tg-best - выход бота, который сторож держит там, где
отвечает Bot API, в том числе на WARP-ноге экзита. Тогда проба мерила путь
через Cloudflare с чужим адресом, и провал WARP красил OpenVPN на здоровой
установке. К адресам самого entry бот не ходит: такой трафик tg-proxy -
только проба, и выход у него - прямые ноги экзитов.

Генератор и матчер правил - из test_router_telegram.py.
"""

import importlib.util
from pathlib import Path
import unittest

from test_router_telegram import generate, route

VERIFY = Path(__file__).resolve().parents[1] / 'lib/verify-traffic.py'
ENTRY_V4, ENTRY_V6 = '203.0.113.10', '2001:db8::10'
ENTRY_ADDRS = [f'{ENTRY_V4}/32', f'{ENTRY_V6}/128']
DIRECT = {'hy2-aa-direct', 'hy2-bb-direct'}


def relay_proxy_port():
    spec = importlib.util.spec_from_file_location('verify_traffic', VERIFY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TG_PROXY[1]


def legs(cfg, tag):
    """Конечные выходы группы: во что группа может переключиться."""
    outbounds = {o['tag']: o for o in cfg['outbounds']}
    members = outbounds[tag].get('outbounds')
    if members is None:
        return {tag}
    return set().union(*(legs(cfg, member) for member in members))


class ExternalProbeExitAddressTest(unittest.TestCase):
    def setUp(self):
        self.cfg = generate('off')
        port = relay_proxy_port()
        tags = [i['tag'] for i in self.cfg['inbounds']
                if i.get('listen') == '127.0.0.1' and i.get('listen_port') == port]
        self.assertEqual(len(tags), 1, f'вход ретранслятора пробы на 127.0.0.1:{port}')
        self.inbound = tags[0]

    def probe(self, cfg, **flow):
        return route(cfg, inbound=self.inbound, source='127.0.0.1', entry_addrs=ENTRY_ADDRS, **flow)

    def test_probe_to_entry_leaves_only_by_direct_exit_legs(self):
        """UDP-порты VPN и TCP-эхо цели - к адресам entry только прямыми ногами экзитов."""
        for mode in ('off', 'direct', 'zapret'):
            cfg = generate(mode)
            for ip in (ENTRY_V4, ENTRY_V6):
                for network, port in (('udp', 1194), ('udp', 51820), ('udp', 4500), ('tcp', 18080)):
                    with self.subTest(mode=mode, ip=ip, network=network, port=port):
                        outbound = self.probe(cfg, ip=ip, network=network, port=port)
                        self.assertNotEqual(outbound, 'reject')
                        self.assertNotEqual(outbound, 'tg-best', 'выход бота: сторож держит его и на WARP')
                        self.assertEqual(legs(cfg, outbound), DIRECT,
                                         f'{outbound} может уйти в {sorted(legs(cfg, outbound) - DIRECT)}')

    def test_bot_traffic_keeps_tg_best(self):
        """Bot API и прочие внешние адреса tg-proxy - по-прежнему через tg-best."""
        for ip, network in (('149.154.167.220', 'tcp'), ('198.51.100.20', 'tcp'), ('198.51.100.20', 'udp')):
            with self.subTest(ip=ip, network=network):
                self.assertEqual(self.probe(self.cfg, ip=ip, network=network, port=443), 'tg-best')

    def test_loopback_through_proxy_stays_closed(self):
        """Через вход пробы loopback entry и экзита закрыт, как и раньше."""
        for port in (9090, 22):
            with self.subTest(port=port):
                self.assertEqual(self.probe(self.cfg, ip='127.0.0.1', network='tcp', port=port), 'reject')


if __name__ == '__main__':
    unittest.main()
