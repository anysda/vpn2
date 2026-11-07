"""Запасная WARP-нога pin-*-warp меняется только после DEAD_AFTER промахов подряд.

Своя нога hy2-aa-warp мертва, pin стоит на запасной hy2-bb-warp, жива и
hy2-cc-warp. Одиночный промах пробы hy2-bb-warp не должен уводить маршрут на
другую ногу: остальные группы сторожа признают выход мёртвым после DEAD_AFTER
промахов подряд, а смена ноги рвёт клиенту новые соединения на другой IP.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('VPN2_AUDIT_ROOT', Path(__file__).resolve().parents[2]))
PIN = 'pin-hy2-aa-warp'
AA_WARP, BB_WARP, CC_WARP = 'hy2-aa-warp', 'hy2-bb-warp', 'hy2-cc-warp'


def watchdog():
    spec = importlib.util.spec_from_file_location('watchdog', ROOT / 'infra/lib/failover-watchdog.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {'DEAD_AFTER': '3'}, clear=True):
        spec.loader.exec_module(module)
    return module


class WarpFallbackDeadAfterTests(unittest.TestCase):
    def setUp(self):
        self.w = watchdog()
        self.proxies = {PIN: {'type': 'Selector', 'now': AA_WARP}}
        self.both = {BB_WARP: 100, CC_WARP: 150}
        self.without_bb = {CC_WARP: 150}

    def run_ticks(self, alive_seq):
        """Тики pins_tick; Selector меняет now по PUT. Возвращает цели PUT."""
        targets = []

        def req(method, path, body=None, **_):
            if method == 'PUT' and path == f'/proxies/{PIN}':
                targets.append(body['name'])
                self.proxies[PIN]['now'] = body['name']
            return {}

        with patch.object(self.w, '_req', side_effect=req), contextlib.redirect_stdout(io.StringIO()):
            for alive in alive_seq:
                self.w._tick_alive = alive
                self.w.pins_tick(self.proxies)
        return targets

    def settle_on_bb(self):
        targets = self.run_ticks([self.both] * self.w.DEAD_AFTER)
        self.assertEqual(self.proxies[PIN]['now'], BB_WARP, targets)

    def test_single_miss_keeps_fallback_leg(self):
        self.settle_on_bb()
        targets = self.run_ticks([self.without_bb, self.both, self.without_bb, self.both])
        self.assertNotIn(CC_WARP, targets,
                         'один промах hy2-bb-warp увёл pin-hy2-aa-warp на hy2-cc-warp')
        self.assertEqual(self.proxies[PIN]['now'], BB_WARP)

    def test_dead_after_misses_switch_fallback_leg(self):
        self.settle_on_bb()
        self.run_ticks([self.without_bb] * (self.w.DEAD_AFTER - 1))
        self.assertEqual(self.proxies[PIN]['now'], BB_WARP,
                         f'pin ушёл с hy2-bb-warp раньше {self.w.DEAD_AFTER} промахов подряд')
        self.run_ticks([self.without_bb])
        self.assertEqual(self.proxies[PIN]['now'], CC_WARP,
                         f'{self.w.DEAD_AFTER} промахов подряд hy2-bb-warp, а pin на нём')


if __name__ == '__main__':
    unittest.main()
