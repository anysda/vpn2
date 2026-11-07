"""Резерв TCP: окно возврата и события узла «UDP недоступен / снова работает».

Выход WARP на экзите шумит: проба канала warp промахивается и по hy2, и по TCP
при живом UDP. Отсюда два требования:
- одиночный промах пробы hy2 у канала на TCP не обнуляет окно TCP_RETURN_S -
  обнуляют только DEAD_AFTER промахов подряд (зеркало правила для TCP-пробы);
- событие узла про UDP решает канал direct: уход warp на TCP из-за плохого
  WARP события не даёт, обрыв UDP на direct даёт одно событие при уходе и одно
  при возврате, где бы ни стоял warp.

clash-api - модель из test_tcp_fallback по настоящему конфигу генератора;
проба выбранного выхода умеет промахиваться всегда (bad) или один раз (miss_once).
"""
import socket
import tempfile
import unittest
import urllib.error
import urllib.parse

import test_tcp_fallback as tf
import test_failover_restart as fr


class NoisyClash(tf.Clash):
    """Проба выходов из bad промахивается всегда, из miss_once - один раз."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.bad = set()
        self.miss_once = set()

    def req(self, method, path, body=None, **kw):
        name = urllib.parse.unquote(path.split('?')[0]).split('/')[2] \
            if path.startswith('/proxies/') else ''
        if method == 'GET' and path.split('?')[0].endswith('/delay'):
            if name in self.bad:
                raise urllib.error.URLError(socket.timeout('timed out'))
            if name in self.miss_once:
                self.miss_once.discard(name)
                raise urllib.error.URLError(socket.timeout('timed out'))
        return super().req(method, path, body, **kw)


class TestTcpNodeEvents(unittest.TestCase):
    def start(self, tmp):
        w = fr.watchdog(tmp)
        clash = NoisyClash(tf.router_config(('aa',)))
        clock = fr.Clock(w.INTERVAL)
        return w, clash, clock, fr.Run(w, clash, clock)

    def on_tcp(self, clash, kind):
        return tf.is_reality(clash.leaf(f'hy2-aa-{kind}'))

    def log(self, run):
        return f'\nсобытия: {run.events}\n' + run.out.getvalue()[-1500:]

    def bad_warp(self, clash, run):
        """WARP на экзите плохой: проба hy2 канала warp мертва, UDP жив."""
        clash.bad.add('transport-hy2-aa-warp')
        run.go(10)
        self.assertTrue(self.on_tcp(clash, 'warp'), 'warp не ушёл на TCP' + self.log(run))
        self.assertFalse(self.on_tcp(clash, 'direct'), 'direct ушёл на TCP при живом UDP'
                         + self.log(run))

    def test_single_hy2_miss_keeps_return_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, clock, run = self.start(tmp)
            clash.udp.discard('aa')
            run.go(10)
            self.assertTrue(self.on_tcp(clash, 'direct'), 'direct не ушёл на TCP' + self.log(run))
            clash.udp.add('aa')
            start = clock.mono
            missed = False
            took = None
            while clock.mono - start < 3 * w.TCP_RETURN:
                if not missed and clock.mono - start >= w.TCP_RETURN / 2:
                    clash.miss_once.add('transport-hy2-aa-direct')
                    missed = True
                run.go(1)
                if not self.on_tcp(clash, 'direct'):
                    took = clock.mono - start
                    break
            self.assertIsNotNone(took, 'direct не вернулся на hy2' + self.log(run))
            self.assertLess(took, w.TCP_RETURN + w.TCP_RETURN / 4,
                            f'один промах пробы hy2 посреди окна, а direct вернулся через '
                            f'{took:.1f} с вместо ~{w.TCP_RETURN:.0f}: окно обнулено без '
                            f'{w.DEAD_AFTER} промахов подряд' + self.log(run))
            self.assertEqual(run.restarts, [])

    def test_warp_only_on_tcp_sends_no_udp_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, clock, run = self.start(tmp)
            self.bad_warp(clash, run)
            run.go(30)
            self.assertEqual(tf.udp_events(run), [],
                             'UDP жив, на TCP ушёл только warp из-за WARP, а событие про UDP '
                             'отправлено' + self.log(run))

    def test_direct_drop_while_warp_on_tcp_sends_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, clock, run = self.start(tmp)
            self.bad_warp(clash, run)
            before = len(tf.udp_events(run))
            clash.udp.discard('aa')
            run.go(10)
            self.assertTrue(self.on_tcp(clash, 'direct'), 'direct не ушёл на TCP' + self.log(run))
            events = tf.udp_events(run)[before:]
            self.assertEqual(len(events), 1, 'обрыв UDP на direct при warp уже на TCP: ждём одно '
                                             'событие «работаю через TCP»' + self.log(run))
            self.assertIn('TCP', events[0])

    def test_direct_return_sends_event_while_warp_on_tcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, clock, run = self.start(tmp)
            self.bad_warp(clash, run)
            clash.udp.discard('aa')
            run.go(10)
            before = len(tf.udp_events(run))
            clash.udp.add('aa')
            run.go(int(3 * w.TCP_RETURN / w.INTERVAL))
            self.assertFalse(self.on_tcp(clash, 'direct'), 'direct не вернулся на hy2'
                             + self.log(run))
            self.assertTrue(self.on_tcp(clash, 'warp'), 'warp при плохом WARP ушёл с TCP'
                            + self.log(run))
            events = tf.udp_events(run)[before:]
            self.assertEqual(len(events), 1, 'direct вернулся на hy2, warp ещё на TCP: ждём одно '
                                             'событие «снова работает»' + self.log(run))
            self.assertIn('снова', events[0])


if __name__ == '__main__':
    unittest.main()
