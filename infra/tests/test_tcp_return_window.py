"""Возврат канала с запасного TCP на Hysteria2 - только по окну TCP_RETURN_S.

Проба канала warp через TCP нестабильна: delay-тест через WARP промахивается
и при рабочем канале. Одиночный промах TCP-пробы не должен сокращать окно
здоровья hy2; раньше окна канал уходит с TCP, только когда TCP-член мёртв
по общему правилу сторожа - DEAD_AFTER промахов подряд.

clash-api - модель из test_tcp_fallback по настоящему конфигу генератора;
проба transport-tcp-* дополнительно умеет промахиваться по расписанию.
"""
import socket
import tempfile
import unittest
import urllib.error
import urllib.parse

import test_tcp_fallback as tf
import test_failover_restart as fr


class FlakyClash(tf.Clash):
    """Проба выбранных TCP-членов промахивается через раз при живом TCP."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.flaky = set()
        self.probes = {}

    def req(self, method, path, body=None, **kw):
        name = urllib.parse.unquote(path.split('?')[0]).split('/')[2] \
            if path.startswith('/proxies/') else ''
        if method == 'GET' and path.split('?')[0].endswith('/delay') and name in self.flaky:
            n = self.probes[name] = self.probes.get(name, 0) + 1
            if n % 2:
                raise urllib.error.URLError(socket.timeout('timed out'))
        return super().req(method, path, body, **kw)


class TestTcpReturnWindow(unittest.TestCase):
    def start(self, tmp):
        w = fr.watchdog(tmp)
        clash = FlakyClash(tf.router_config(('aa',)))
        clock = fr.Clock(w.INTERVAL)
        return w, clash, clock, fr.Run(w, clash, clock)

    def to_tcp(self, clash, run):
        clash.udp.discard('aa')
        run.go(10)
        for kind in tf.KINDS:
            self.assertTrue(tf.is_reality(clash.leaf(f'hy2-aa-{kind}')),
                            f'{kind}: канал не ушёл на TCP\n' + run.out.getvalue()[-1500:])

    def back_at(self, clash, clock, run, kind, limit=120):
        """Секунды от clock.mono до возврата канала kind на hy2 (None - не вернулся)."""
        start = clock.mono
        while clock.mono - start < limit:
            run.go(1)
            if clash.leaf(f'hy2-aa-{kind}')['type'] == 'hysteria2':
                return clock.mono - start
        return None

    def test_flaky_tcp_probe_does_not_shorten_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, clock, run = self.start(tmp)
            self.to_tcp(clash, run)
            clash.flaky.add('transport-tcp-aa-warp')
            clash.udp.add('aa')
            took = self.back_at(clash, clock, run, 'warp')
            log = run.out.getvalue()[-1500:]
            self.assertIsNotNone(took, 'warp не вернулся на hy2 и после окна\n' + log)
            self.assertGreaterEqual(took, w.TCP_RETURN,
                                    f'warp вернулся на hy2 через {took:.1f} с, раньше окна '
                                    f'{w.TCP_RETURN:.0f} с, из-за промаха TCP-пробы\n' + log)
            self.assertEqual(run.restarts, [])

    def test_hy2_drop_inside_window_restarts_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, clock, run = self.start(tmp)
            self.to_tcp(clash, run)
            clash.flaky.add('transport-tcp-aa-warp')
            clash.udp.add('aa')
            run.go(int(w.TCP_RETURN / 2 / w.INTERVAL))
            clash.udp.discard('aa')
            run.go(2)
            clash.udp.add('aa')
            took = self.back_at(clash, clock, run, 'warp')
            log = run.out.getvalue()[-1500:]
            self.assertIsNotNone(took, 'warp не вернулся на hy2 и после окна\n' + log)
            self.assertGreaterEqual(took, w.TCP_RETURN,
                                    f'hy2 падал внутри окна, а warp вернулся через {took:.1f} с '
                                    'после его возврата: отсчёт не начат сначала\n' + log)

    def test_dead_tcp_with_live_hy2_returns_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, clock, run = self.start(tmp)
            self.to_tcp(clash, run)
            clash.udp.add('aa')
            clash.tcp.discard('aa')
            took = self.back_at(clash, clock, run, 'warp')
            log = run.out.getvalue()[-1500:]
            self.assertIsNotNone(took, 'TCP мёртв, hy2 жив, а канал стоит на мёртвом TCP\n' + log)
            self.assertLess(took, w.TCP_RETURN,
                            f'TCP мёртв ({w.DEAD_AFTER} промахов подряд), hy2 жив, а возврат '
                            f'через {took:.1f} с - ждали окно при мёртвом канале\n' + log)
            self.assertEqual(run.restarts, [])


if __name__ == '__main__':
    unittest.main()
