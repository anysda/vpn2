"""VPN2-649: лечение выхода ручного маршрута, зависшего при живом узле.

Выход не оживает и после перезапуска — сторож лечит его не больше двух раз и
шлёт одно событие. Узел мёртв целиком — перезапуска нет, pin на foreign-best.
"""
import tempfile
import unittest

from test_failover_restart import Clash, Clock, Run, watchdog


PIN = 'pin-hy2-aa-direct'
STUCK = 'hy2-aa-direct'


def pinned(alive):
    clash = Clash(['aa', 'bb'])
    clash.alive = set(alive)
    clash.now[PIN] = STUCK
    return clash


class TestStuckExit(unittest.TestCase):
    def test_exit_that_never_revives_is_healed_twice_then_given_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = watchdog(tmp, INTERVAL='2')
            clash = pinned(set(Clash(['aa', 'bb']).tags) - {STUCK})
            run = Run(w, clash, Clock(w.INTERVAL)).go(int(3600 / w.INTERVAL))
            self.assertEqual(len(run.restarts), 2,
                             f'час зависшего выхода: ждём два перезапуска, было {len(run.restarts)}')
            self.assertGreaterEqual(run.restarts[1] - run.restarts[0], 600)
            given_up = [e for e in run.events if 'не оживает' in e[0][1]]
            self.assertEqual(len(given_up), 1, f'событие «не оживает» — одно: {run.events}')
            self.assertEqual(clash.now[PIN], 'foreign-best')

    def test_dead_node_keeps_pin_on_fallback_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = watchdog(tmp, INTERVAL='2')
            clash = pinned({'hy2-bb-direct', 'hy2-bb-warp'})
            run = Run(w, clash, Clock(w.INTERVAL)).go(int(1800 / w.INTERVAL))
            self.assertEqual(run.restarts, [], 'узел aa мёртв целиком: перезапуск не лечит')
            self.assertEqual(clash.now[PIN], 'foreign-best')

    def test_alive_transport_counts_as_alive_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = watchdog(tmp, INTERVAL='2')
            clash = pinned({'hy2-bb-direct', 'hy2-bb-warp', f'transport-{STUCK}'})
            clash.now[f'transport-{STUCK}'] = f'transport-{STUCK}'
            run = Run(w, clash, Clock(w.INTERVAL)).go(int(300 / w.INTERVAL))
            self.assertEqual(len(run.restarts), 1, 'transport выхода отвечает: узел жив, ждём перезапуск')

    def warp_down(self, transport_alive):
        with tempfile.TemporaryDirectory() as tmp:
            w = watchdog(tmp, INTERVAL='2')
            clash = Clash(['aa', 'bb'])
            for t in clash.tags:
                clash.now[f'pin-{t}'] = t
                clash.now[f'transport-{t}'] = f'transport-{t}'
            clash.alive = (set(clash.tags) | {f'transport-{t}' for t in clash.tags}) - {'hy2-aa-warp'}
            if not transport_alive:
                clash.alive.discard('transport-hy2-aa-warp')
            return Run(w, clash, Clock(w.INTERVAL)).go(int(3600 / w.INTERVAL))

    def test_dead_warp_leg_with_live_direct_is_not_healed(self):
        run = self.warp_down(transport_alive=False)
        self.assertEqual(run.restarts, [], 'упал WARP (transport warp мёртв, direct жив): '
                                           f'перезапуск хаба WARP не вернёт, было {len(run.restarts)}')

    def test_stuck_warp_exit_with_live_transport_is_healed(self):
        run = self.warp_down(transport_alive=True)
        self.assertEqual(len(run.restarts), 2, 'warp-выход завис при живом transport: ждём лечение')


if __name__ == '__main__':
    unittest.main()
