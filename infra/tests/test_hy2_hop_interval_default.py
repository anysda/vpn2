"""Умолчание смены портов hy2 не держит ногу на мёртвом потоке ~30 с.

Сеть изредка роняет новый UDP-поток после смены порта: ответов нет ни на один
пакет, а клиент hysteria2 меняет порт снова только по таймеру hop_interval.
Без ports.hy2_hop.interval в config.yaml у каждой hy2-ноги entry (direct, warp,
mgmt) интервал не больше 5 с; явный интервал из конфига берётся как есть,
при enabled: false смены портов нет вовсе.
"""
import re
import unittest

import test_hy2_port_hopping as hopping
from test_hy2_port_hopping import HOP_KEYS, old_config, with_hop

LIMIT_SECONDS = 5


def seconds(value):
    match = re.fullmatch(r'([1-9][0-9]*)([smh])', value or '')
    if not match:
        raise AssertionError(f'hop_interval не длительность: {value!r}')
    return int(match[1]) * {'s': 1, 'm': 60, 'h': 3600}[match[2]]


class Hy2HopIntervalDefaultTests(unittest.TestCase):
    # Тот же путь config.yaml → переменные узла → генератор, что у test_hy2_port_hopping.
    entry = hopping.Hy2PortHoppingTests.entry

    def assert_short_hops(self, config):
        outbounds, _, _ = self.entry(config)
        for (tag, channel), outbound in sorted(outbounds.items()):
            with self.subTest(exit=tag, channel=channel):
                self.assertIn('server_ports', outbound)
                self.assertLessEqual(seconds(outbound.get('hop_interval')), LIMIT_SECONDS,
                                     f'{outbound["tag"]}: мёртвый поток живёт до следующей смены порта')

    def test_config_without_hop_block_hops_within_five_seconds(self):
        self.assert_short_hops(old_config())

    def test_hop_block_without_interval_hops_within_five_seconds(self):
        self.assert_short_hops(with_hop('true'))

    def test_explicit_interval_is_taken_as_is(self):
        outbounds, _, _ = self.entry(with_hop('true', '45s'))
        for outbound in outbounds.values():
            self.assertEqual(outbound.get('hop_interval'), '45s', outbound['tag'])

    def test_disabled_hopping_has_no_hop_keys(self):
        outbounds, _, _ = self.entry(with_hop('false'))
        for outbound in outbounds.values():
            for key in HOP_KEYS:
                self.assertNotIn(key, outbound, outbound['tag'])


if __name__ == '__main__':
    unittest.main()
