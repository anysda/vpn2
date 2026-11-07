"""Резерв TCP: мёртвый hy2 канала warp при живом direct не штрафует узел.

Обрыв UDP до экзита: direct уходит на TCP, а проба канала warp через TCP шумит
(WARP) и может промахнуться в том же тике. Раньше канал warp оставался на
мёртвом hy2, основной сторож видел мёртвый hy2-<узел>-warp, ставил весь узел в
штрафную, и дорожка pin-hy2-<узел>-direct уходила на foreign-best, хотя direct
узла жив через TCP. Требования:
- промах TCP-пробы warp при мёртвом hy2 до-проверяется, канал уходит на TCP
  в том же тике, что и direct;
- смерть выхода warp при живом direct того же узла узел не штрафует и дорожки
  direct не двигает;
- узел мёртв целиком (UDP и TCP) - штраф и уход дорожек как раньше.

clash-api - модель из test_tcp_fallback по настоящему конфигу генератора.
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


class TestWarpDeadNodePenalty(unittest.TestCase):
    def start(self, tmp):
        w = fr.watchdog(tmp)
        clash = NoisyClash(tf.router_config(('aa', 'bb')))
        run = fr.Run(w, clash, fr.Clock(w.INTERVAL))
        run.go(3)
        # основной выбор на warp узла aa, как в журнале со стенда
        clash.now['foreign-best'] = 'hy2-aa-warp'
        run.go(2)
        return w, clash, run

    def on_tcp(self, clash, kind):
        return tf.is_reality(clash.leaf(f'hy2-aa-{kind}'))

    def log(self, run):
        return '\n' + run.out.getvalue()[-2500:]

    def pin_moves(self, clash, start):
        return [p for p in clash.puts[start:] if p[0] == 'pin-hy2-aa-direct']

    def test_warp_tcp_miss_rechecked_with_direct(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, run = self.start(tmp)
            mark = len(clash.puts)
            clash.miss_once.add('transport-tcp-aa-warp')
            clash.udp.discard('aa')
            for _ in range(10):
                run.go(1)
                if self.on_tcp(clash, 'direct'):
                    break
            self.assertTrue(self.on_tcp(clash, 'direct'), 'direct не ушёл на TCP' + self.log(run))
            self.assertTrue(self.on_tcp(clash, 'warp'),
                            'hy2 warp мёртв, TCP-проба warp промахнулась один раз, а канал '
                            'остался на мёртвом hy2 без до-проверки TCP' + self.log(run))
            run.go(15)
            self.assertNotIn('узел aa в штрафной', run.out.getvalue(),
                             'direct узла жив через TCP, а узел оштрафован' + self.log(run))
            self.assertEqual(self.pin_moves(clash, mark), [],
                             'дорожка direct ушла с живого узла' + self.log(run))

    def test_dead_warp_with_live_direct_keeps_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, run = self.start(tmp)
            mark = len(clash.puts)
            clash.bad.add('transport-tcp-aa-warp')
            clash.udp.discard('aa')
            run.go(20)
            self.assertTrue(self.on_tcp(clash, 'direct'), 'direct не ушёл на TCP' + self.log(run))
            self.assertNotIn('узел aa в штрафной', run.out.getvalue(),
                             'мёртв только выход warp, direct узла жив через TCP, а узел '
                             'оштрафован' + self.log(run))
            self.assertEqual(self.pin_moves(clash, mark), [],
                             'выход warp мёртв, а дорожка direct ушла на foreign-best'
                             + self.log(run))
            self.assertNotEqual(clash.now['foreign-best'], 'hy2-aa-warp',
                                'основной выбор остался на мёртвом warp' + self.log(run))
            self.assertEqual(clash.now['pin-hy2-aa-warp'], 'hy2-bb-warp',
                             'дорожка warp не ушла на живую ногу WARP' + self.log(run))

    def test_whole_node_dead_penalized_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            w, clash, run = self.start(tmp)
            mark = len(clash.puts)
            clash.udp.discard('aa')
            clash.tcp.discard('aa')
            run.go(10)
            self.assertIn('узел aa в штрафной', run.out.getvalue(),
                          'узел мёртв целиком, а штрафа нет' + self.log(run))
            self.assertIn(('pin-hy2-aa-direct', 'foreign-best'), self.pin_moves(clash, mark),
                          'узел мёртв целиком, а дорожка direct не ушла' + self.log(run))


if __name__ == '__main__':
    unittest.main()
